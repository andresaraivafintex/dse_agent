"""WSF-E8-T2 — Retenção por classificação de dados (§12.2).

Política por tenant/classe em `tenant_config.retention` (JSONB, migração
reservada `migrations/0018_wsf3.sql`):

    {"internal": {"days": 90}, "restricted": {"days": 30}}

Semântica (deliberadamente conservadora):

- Classe SEM entrada na política => NADA é expurgado para aquela classe.
  Retenção é decisão explícita por tenant — nunca um default silencioso.
- `days` conta a partir de `received_at` (ingest) / `created_at` (artifacts).

Alvos:

1. ``ingest_events.payload`` (contém o `content_snapshot` do usuário —
   FR-06): **anonimização** via UPDATE para um tombstone JSONB que preserva
   os metadados de correlação (`kind`, `event_id`, work_item) mas remove o
   conteúdo classificado. DELETE físico é estruturalmente impossível para a
   role de app (`dse_app` só tem SELECT/UPDATE em ingest_events —
   0001_foundation.sql) e indesejável (outbox com FK; a linha é evidência de
   QUE algo chegou; o payload é o dado classificado). Só toca linhas com
   `processed = true` — o dispatcher ainda precisa das não processadas.
2. Artifacts do store (WS-E, Fase 3, tabela em construção em paralelo):
   **expurgo**. O alvo só roda se a tabela existir E tiver as colunas
   mínimas documentadas (`tenant_id`, `data_class`, `created_at`) — caso
   contrário é reportado como `skipped` com razão explícita (nunca
   silencioso). O nome da tabela é configurável (`DSE_ARTIFACT_TABLE`,
   default ``wse_artifacts``) — combinar com o WS-E na integração.

audit_log NUNCA é alvo (append-only, retenção compliance-grade própria):
além da garantia estrutural do schema (REVOKE UPDATE/DELETE), este módulo
recusa qualquer alvo cujo nome comece com ``audit_log`` (defesa em
profundidade, coberto por teste).

P1: toda decisão aqui é comparação de timestamps/strings em código.
P6: política malformada => ValueError na borda, nunca um expurgo parcial.
P8: toda execução (incluindo dry-run) grava linha de audit via
    ``dse_audit.emit`` na MESMA transação da mutação.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
from typing import Any

import psycopg2
import psycopg2.extras
from dse_audit import emit

from .tenant_config import _get_connection

# Classes de dados conhecidas (mesmo vocabulário de work_items.data_class /
# gateway_contract.data_class). Aberto por design — uma classe nova não exige
# migração — mas o shape da política é validado.
TOMBSTONE_MARKER = "dse_retention_purged"

# Defesa em profundidade: além do REVOKE estrutural no schema, nenhum código
# deste módulo aceita um alvo de audit ledger.
_FORBIDDEN_TARGET_PREFIX = "audit_log"

# Shape real de migrations/0017_wse3.sql (WS-E): a classe do dado NÃO está na
# tabela de artifacts — vem do work_item que gerou a evidência (JOIN).
_ARTIFACT_TABLE = os.environ.get("DSE_ARTIFACT_TABLE", "wse_artifacts")
_ARTIFACT_REQUIRED_COLUMNS = {"tenant_id", "work_item_id", "created_at"}


class RetentionPolicyError(ValueError):
    """Política de retenção malformada — falha limpa na borda (P6)."""


@dataclasses.dataclass(frozen=True)
class RetentionPolicy:
    data_class: str
    days: int


@dataclasses.dataclass
class TargetResult:
    target: str            # 'ingest_events' | tabela de artifacts | ...
    data_class: str
    action: str            # 'anonymize' | 'purge' | 'skipped'
    cutoff: dt.datetime | None
    candidates: int
    affected: int
    reason: str | None = None   # preenchido quando action == 'skipped'
    # bucket/store_key dos artifacts deletados — o cleanup do objeto S3
    # correspondente (Garage) é do lifecycle do WS-E; a lista vai no audit
    # row para o compensatório nunca ser silencioso.
    purged_store_keys: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class RetentionReport:
    tenant_id: str
    dry_run: bool
    results: list[TargetResult] = dataclasses.field(default_factory=list)

    @property
    def total_affected(self) -> int:
        return sum(r.affected for r in self.results)


# ---------------------------------------------------------------------------
# Política (leitura/escrita em tenant_config.retention)
# ---------------------------------------------------------------------------
def _parse_policies(raw: Any) -> dict[str, RetentionPolicy]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise RetentionPolicyError(f"tenant_config.retention deve ser um objeto JSON, veio {type(raw).__name__}")
    policies: dict[str, RetentionPolicy] = {}
    for data_class, spec in raw.items():
        if not isinstance(spec, dict) or "days" not in spec:
            raise RetentionPolicyError(
                f"política da classe '{data_class}' deve ter shape {{\"days\": int}}, veio: {spec!r}"
            )
        days = spec["days"]
        if not isinstance(days, int) or isinstance(days, bool) or days <= 0:
            raise RetentionPolicyError(
                f"política da classe '{data_class}': days deve ser inteiro > 0, veio: {days!r}"
            )
        policies[data_class] = RetentionPolicy(data_class=data_class, days=days)
    return policies


def get_retention_policies(tenant_id: str, conn=None) -> dict[str, RetentionPolicy]:
    """Política efetiva do tenant por data_class. Dict vazio = nenhuma
    retenção configurada = nada é expurgado (conservador).

    Contrato de consumo cross-workstream: o job de lifecycle do artifact
    store (WS-E) deve ler a política DAQUI (fonte única), nunca duplicá-la.
    """
    owns_conn = conn is None
    if owns_conn:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT retention FROM tenant_config WHERE tenant_id = %s", (tenant_id,))
            row = cur.fetchone()
    finally:
        if owns_conn:
            conn.close()
    return _parse_policies(row[0] if row else None)


def set_retention_policy(
    tenant_id: str,
    data_class: str,
    *,
    days: int | None,
    actor: str,
    conn=None,
) -> dict[str, RetentionPolicy]:
    """Define (days=int) ou remove (days=None) a política de uma classe.
    Valida o shape ANTES de gravar e audita a mudança (P8) na mesma
    transação. Cria a linha de tenant_config se ainda não existir."""
    if days is not None and (not isinstance(days, int) or isinstance(days, bool) or days <= 0):
        raise RetentionPolicyError(f"days deve ser inteiro > 0 ou None (remover), veio: {days!r}")

    owns_conn = conn is None
    if owns_conn:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            if days is None:
                cur.execute(
                    """
                    INSERT INTO tenant_config (tenant_id) VALUES (%s)
                    ON CONFLICT (tenant_id) DO UPDATE SET retention = tenant_config.retention - %s
                    """,
                    (tenant_id, data_class),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO tenant_config (tenant_id, retention)
                    VALUES (%s, jsonb_build_object(%s, jsonb_build_object('days', %s::int)))
                    ON CONFLICT (tenant_id) DO UPDATE SET
                        retention = tenant_config.retention
                                    || jsonb_build_object(%s, jsonb_build_object('days', %s::int))
                    """,
                    (tenant_id, data_class, days, data_class, days),
                )
        emit(
            actor=actor,
            action="retention_policy_set" if days is not None else "retention_policy_removed",
            tenant_id=tenant_id,
            details={"data_class": data_class, "days": days},
            conn=conn,
        )
        if owns_conn:
            conn.commit()
    except Exception:
        if owns_conn:
            conn.rollback()
        raise
    finally:
        if owns_conn:
            conn.close()
    return get_retention_policies(tenant_id)


# ---------------------------------------------------------------------------
# Execução
# ---------------------------------------------------------------------------
def _assert_target_allowed(table: str) -> None:
    if table.strip().lower().startswith(_FORBIDDEN_TARGET_PREFIX):
        raise RetentionPolicyError(
            "audit_log NUNCA é alvo de retenção deste job — o ledger é append-only "
            "com retenção compliance-grade própria (WSF-E8-T2/§12.2)."
        )


def _tombstone(policy: RetentionPolicy, executed_at: dt.datetime) -> dict[str, Any]:
    return {
        TOMBSTONE_MARKER: True,
        "data_class": policy.data_class,
        "retention_days": policy.days,
        "purged_at": executed_at.isoformat(),
    }


def _run_ingest_events_target(
    cur,
    tenant_id: str,
    policy: RetentionPolicy,
    cutoff: dt.datetime,
    executed_at: dt.datetime,
    dry_run: bool,
) -> TargetResult:
    _assert_target_allowed("ingest_events")
    # Candidatos: eventos JÁ processados, mais velhos que o cutoff, da classe
    # do work_item correspondente, ainda não tombstonados (idempotência).
    cur.execute(
        """
        SELECT count(*) FROM ingest_events e
        JOIN work_items w ON w.id = e.work_item_id
        WHERE w.tenant_id = %s
          AND w.data_class = %s
          AND e.processed
          AND e.received_at < %s
          AND NOT (e.payload ? %s)
        """,
        (tenant_id, policy.data_class, cutoff, TOMBSTONE_MARKER),
    )
    candidates = cur.fetchone()[0]
    affected = 0
    if not dry_run and candidates:
        cur.execute(
            """
            UPDATE ingest_events e
            SET payload = %s::jsonb
            FROM work_items w
            WHERE w.id = e.work_item_id
              AND w.tenant_id = %s
              AND w.data_class = %s
              AND e.processed
              AND e.received_at < %s
              AND NOT (e.payload ? %s)
            """,
            (json.dumps(_tombstone(policy, executed_at)), tenant_id, policy.data_class, cutoff, TOMBSTONE_MARKER),
        )
        affected = cur.rowcount
    return TargetResult(
        target="ingest_events",
        data_class=policy.data_class,
        action="anonymize",
        cutoff=cutoff,
        candidates=candidates,
        affected=affected,
    )


def _artifact_table_shape(cur, table: str) -> set[str]:
    cur.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s",
        (table,),
    )
    return {r[0] for r in cur.fetchall()}


def _run_artifacts_target(
    cur,
    tenant_id: str,
    policy: RetentionPolicy,
    cutoff: dt.datetime,
    dry_run: bool,
    table: str,
) -> TargetResult:
    _assert_target_allowed(table)
    columns = _artifact_table_shape(cur, table)
    if not columns:
        return TargetResult(
            target=table, data_class=policy.data_class, action="skipped", cutoff=cutoff,
            candidates=0, affected=0,
            reason=f"tabela '{table}' não existe (artifact store do WS-E ainda não integrado)",
        )
    missing = _ARTIFACT_REQUIRED_COLUMNS - columns
    if missing:
        return TargetResult(
            target=table, data_class=policy.data_class, action="skipped", cutoff=cutoff,
            candidates=0, affected=0,
            reason=f"tabela '{table}' sem colunas mínimas {sorted(missing)} — combinar shape com WS-E",
        )
    # psycopg2 não parametriza identificadores — o nome vem de env/parâmetro
    # interno, já validado contra o prefixo proibido; ainda assim, cite-o.
    from psycopg2 import sql as _sql

    # Artifacts QUARENTENADOS nunca são expurgados por retenção: quarentena é
    # evidência retida para investigação (WSE-E5-T12 / WS-F Fase 2) — só sai
    # por decisão de operador, não por política de idade.
    quarantine_guard = (
        _sql.SQL(" AND a.quarantined_at IS NULL") if "quarantined_at" in columns else _sql.SQL("")
    )
    where = _sql.SQL(
        "FROM {t} a JOIN work_items w ON w.id = a.work_item_id "
        "WHERE a.tenant_id = %s AND w.data_class = %s AND a.created_at < %s"
    ).format(t=_sql.Identifier(table)) + quarantine_guard

    cur.execute(_sql.SQL("SELECT count(*) ") + where, (tenant_id, policy.data_class, cutoff))
    candidates = cur.fetchone()[0]

    if not dry_run:
        # Pré-flight de privilégio (evita abortar a transação no meio — P6):
        # a migração do WS-E (0017) não dá DELETE em wse_artifacts à role de
        # app. Até o grant ser combinado na integração, o expurgo real fica
        # explicitamente skipped (o dry-run/contagem funciona só com SELECT).
        cur.execute("SELECT has_table_privilege(current_user, %s, 'DELETE')", (table,))
        if not cur.fetchone()[0]:
            return TargetResult(
                target=table, data_class=policy.data_class, action="skipped", cutoff=cutoff,
                candidates=candidates, affected=0,
                reason=(
                    f"role atual sem GRANT DELETE em '{table}' (0017_wse3.sql) — "
                    "pedido registrado no README do WS-F para o WS-E conceder na integração"
                ),
            )

    affected = 0
    purged_keys: list[str] = []
    if not dry_run and candidates:
        # Captura bucket/store_key ANTES do DELETE: o objeto S3 correspondente
        # (Garage) precisa ser removido pelo lifecycle do WS-E — a lista sai
        # no audit row para o cleanup compensatório nunca ser silencioso.
        returning = (
            _sql.SQL(" RETURNING a.bucket || '/' || a.store_key")
            if {"bucket", "store_key"} <= columns
            else _sql.SQL(" RETURNING a.work_item_id")
        )
        cur.execute(
            _sql.SQL("DELETE FROM {t} a USING work_items w "
                     "WHERE w.id = a.work_item_id AND a.tenant_id = %s AND w.data_class = %s "
                     "AND a.created_at < %s").format(t=_sql.Identifier(table))
            + quarantine_guard + returning,
            (tenant_id, policy.data_class, cutoff),
        )
        purged_keys = [r[0] for r in cur.fetchall()]
        affected = len(purged_keys)
    return TargetResult(
        target=table, data_class=policy.data_class, action="purge", cutoff=cutoff,
        candidates=candidates, affected=affected, purged_store_keys=purged_keys,
    )


def run_retention(
    tenant_id: str,
    *,
    dry_run: bool = False,
    actor: str = "system:retention-job",
    now: dt.datetime | None = None,
    artifact_table: str | None = None,
    conn=None,
) -> RetentionReport:
    """Executa (ou simula, com ``dry_run=True``) a retenção de UM tenant.

    Uma transação única: mutações + linha de audit são atômicas (P8). Sem
    política configurada => no-op auditado como zero alvos (nunca expurgo
    implícito)."""
    executed_at = now or dt.datetime.now(dt.timezone.utc)
    table = artifact_table or _ARTIFACT_TABLE
    report = RetentionReport(tenant_id=tenant_id, dry_run=dry_run)

    owns_conn = conn is None
    if owns_conn:
        conn = _get_connection()
    try:
        policies = get_retention_policies(tenant_id, conn=conn)
        with conn.cursor() as cur:
            for policy in policies.values():
                cutoff = executed_at - dt.timedelta(days=policy.days)
                report.results.append(
                    _run_ingest_events_target(cur, tenant_id, policy, cutoff, executed_at, dry_run)
                )
                report.results.append(
                    _run_artifacts_target(cur, tenant_id, policy, cutoff, dry_run, table)
                )
        emit(
            actor=actor,
            action="retention_dry_run" if dry_run else "retention_executed",
            tenant_id=tenant_id,
            details={
                "executed_at": executed_at.isoformat(),
                "policies": {p.data_class: p.days for p in policies.values()},
                "results": [
                    {
                        "target": r.target,
                        "data_class": r.data_class,
                        "action": r.action,
                        "cutoff": r.cutoff.isoformat() if r.cutoff else None,
                        "candidates": r.candidates,
                        "affected": r.affected,
                        "reason": r.reason,
                        "purged_store_keys": r.purged_store_keys,
                    }
                    for r in report.results
                ],
            },
            conn=conn,
        )
        if owns_conn:
            conn.commit()
    except Exception:
        if owns_conn:
            conn.rollback()
        raise
    finally:
        if owns_conn:
            conn.close()
    return report


def run_retention_all_tenants(
    *,
    dry_run: bool = False,
    actor: str = "system:retention-job",
    now: dt.datetime | None = None,
) -> list[RetentionReport]:
    """Varre todos os tenants com política configurada (job agendado).

    Falha de UM tenant (ex.: política corrompida) NÃO aborta a varredura dos
    demais — aquele tenant fica sem expurgo (rollback limpo, P6), a falha é
    auditada (`retention_failed`, P8) e a varredura continua. Achado real:
    a primeira versão abortava a sweep inteira no primeiro tenant com JSONB
    malformado."""
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT tenant_id FROM tenant_config WHERE retention <> '{}'::jsonb ORDER BY tenant_id")
            tenant_ids = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()

    reports: list[RetentionReport] = []
    for tenant_id in tenant_ids:
        try:
            reports.append(run_retention(tenant_id, dry_run=dry_run, actor=actor, now=now))
        except Exception as exc:  # noqa: BLE001 — isolamento por tenant; a falha é auditada, nunca engolida
            emit(
                actor=actor,
                action="retention_failed",
                tenant_id=tenant_id,
                details={"error": str(exc), "dry_run": dry_run},
            )
    return reports
