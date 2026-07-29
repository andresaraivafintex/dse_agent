"""DURABLE cost ledger (WSD-E3-T4).

In Phase 1 the per-call cost only existed in the current process's
`InMemorySpanExporter` — it vanished on restart and did not aggregate across
processes. Phase 2 writes every successful call into a Postgres table
(`model_call_ledger`, migrations/0011_wsd2.sql) with the REAL cost/tokens
LiteLLM returns. That table is:

  1. the queryable, DURABLE source for `cost_export` (survives a restart), and
  2. the "spent-so-far" accounting for call-time budget enforcement
     (`budget.py`).

OTel spans keep being exported in parallel to the WS-F collector
(`telemetry.py`) — but the local collector only does `debug`/stdout (no
queryable backend in this environment), so the durable aggregation lives here,
not in the collector. See README §"WSD-E3-T4".
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
) -> int:
    """Writes an immutable row into the ledger and RETURNS its id. Called by
    `gateway_call.chat_completion` AFTER a 2xx response (only real cost goes
    into the ledger — a denied/failed call produces no row here), and by the
    coder Activity, whose spend never passes through the gateway client at all.

    The id is returned so the caller can record WHICH ledger row represents a
    turn: the console projector needs that to tell a metered turn from a legacy
    one and avoid counting the same money twice."""
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO model_call_ledger
                    (tenant_id, work_item_id, stage, task_class, model,
                     cost_usd, tokens_in, tokens_out)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
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
            new_id = cur.fetchone()[0]
        conn.commit()
        return new_id
    finally:
        conn.close()


def aggregate(*, tenant_id: str | None = None) -> list[dict]:
    """Aggregates cost/tokens by (tenant_id, task_class, stage) from the
    durable table. Replaces Phase 1's read of the in-memory buffer — the result
    survives a process restart and aggregates across processes."""
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
    """Accumulated cost (USD) for a WorkItem — used by budget enforcement."""
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
    """Accumulated cost (USD) for the tenant in the current month (UTC) — the
    aggregate budget."""
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
