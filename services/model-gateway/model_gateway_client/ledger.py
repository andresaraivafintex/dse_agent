"""Ledger de custo DURÁVEL (WSD-E3-T4).

Na Fase 1 o custo por chamada só existia num `InMemorySpanExporter` do processo
atual — sumia em restart e não agregava entre processos. A Fase 2 grava cada
chamada bem-sucedida numa tabela Postgres (`model_call_ledger`,
migrations/0011_wsd2.sql) com o custo/tokens REAIS que o LiteLLM devolve. Essa
tabela é:

  1. a fonte consultável e DURÁVEL do `cost_export` (sobrevive a restart), e
  2. o accounting de "spent-so-far" do enforcement de budget no call time
     (`budget.py`).

Os spans OTel continuam sendo exportados em paralelo para o collector do WS-F
(`telemetry.py`) — mas o collector local só faz `debug`/stdout (sem backend
consultável neste ambiente), então a agregação durável mora aqui, não no
collector. Ver README §"WSD-E3-T4".
"""
from __future__ import annotations

from dataclasses import dataclass

from . import db


@dataclass(frozen=True)
class LedgerAggregateRow:
    tenant_id: str
    task_class: str
    stage: str
    call_count: int
    total_cost_usd: float
    total_tokens_in: int
    total_tokens_out: int
    models: list[str]

    def as_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "task_class": self.task_class,
            "stage": self.stage,
            "call_count": self.call_count,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "total_tokens_in": self.total_tokens_in,
            "total_tokens_out": self.total_tokens_out,
            "models": self.models,
        }


def record_call(
    *,
    tenant_id: str,
    work_item_id: str,
    stage: str,
    task_class: str,
    model: str,
    cost_usd: float,
    tokens_in: int,
    tokens_out: int,
) -> None:
    """Grava uma linha imutável no ledger. Chamado por
    `gateway_call.chat_completion` DEPOIS de uma resposta 2xx (só custo real
    entra no ledger — uma chamada negada/falha não gera linha aqui)."""
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO model_call_ledger
                    (tenant_id, work_item_id, stage, task_class, model,
                     cost_usd, tokens_in, tokens_out)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    tenant_id,
                    work_item_id,
                    stage,
                    task_class,
                    model,
                    cost_usd,
                    tokens_in,
                    tokens_out,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def aggregate(*, tenant_id: str | None = None) -> list[dict]:
    """Agrega custo/tokens por (tenant_id, task_class, stage) a partir da
    tabela durável. Substitui a leitura do buffer em memória da Fase 1 — o
    resultado sobrevive a restart do processo e agrega entre processos."""
    where = ""
    params: tuple = ()
    if tenant_id is not None:
        where = "WHERE tenant_id = %s"
        params = (tenant_id,)

    sql = f"""
        SELECT tenant_id, task_class, stage,
               COUNT(*)                AS call_count,
               COALESCE(SUM(cost_usd), 0)   AS total_cost_usd,
               COALESCE(SUM(tokens_in), 0)  AS total_tokens_in,
               COALESCE(SUM(tokens_out), 0) AS total_tokens_out,
               ARRAY_AGG(DISTINCT model)    AS models
          FROM model_call_ledger
          {where}
      GROUP BY tenant_id, task_class, stage
      ORDER BY tenant_id, task_class, stage
    """
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()

    out: list[dict] = []
    for r in rows:
        out.append(
            LedgerAggregateRow(
                tenant_id=r[0],
                task_class=r[1],
                stage=r[2],
                call_count=int(r[3]),
                total_cost_usd=float(r[4]),
                total_tokens_in=int(r[5]),
                total_tokens_out=int(r[6]),
                models=sorted(r[7] or []),
            ).as_dict()
        )
    return out


def spent_for_work_item(work_item_id: str) -> float:
    """Custo acumulado (USD) de um WorkItem — usado pelo enforcement de budget."""
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM model_call_ledger WHERE work_item_id = %s",
                (work_item_id,),
            )
            return float(cur.fetchone()[0])
    finally:
        conn.close()


def spent_for_tenant_this_month(tenant_id: str) -> float:
    """Custo acumulado (USD) do tenant no mês corrente (UTC) — budget agregado."""
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(SUM(cost_usd), 0)
                  FROM model_call_ledger
                 WHERE tenant_id = %s
                   AND created_at >= date_trunc('month', now())
                """,
                (tenant_id,),
            )
            return float(cur.fetchone()[0])
    finally:
        conn.close()
