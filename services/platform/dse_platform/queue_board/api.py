"""WSF-E6-T1 — API do queue board sobre o Postgres (system of record).

Projeções somente-leitura sobre `work_items` (a máquina de estados §9.3 é a
fonte da verdade — o board NÃO consulta o Temporal para o estado corrente, só
para controles em operator.py) + `tenant_config` (budgets) + `audit_log`
(trilha + custo corrente agregado) + `dse_work_item_quarantine`.

Deliberadamente cru: dataclasses + funções puras de query. A projeção pública
grosseira reusa `dse_contracts.work_item.to_public_status` (contrato já testado)
para não redefinir o mapeamento estado-interno -> estado-público.
"""
from __future__ import annotations

import dataclasses
import os
from decimal import Decimal
from typing import Any

import psycopg2
import psycopg2.extras

from dse_contracts.work_item import WorkItemStatus, to_public_status

_DSN = os.environ.get(
    "DSE_PLATFORM_DATABASE_URL",
    os.environ.get("DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"),
)

# Todos os estados da máquina §9.3 (ordem de exibição no board).
ALL_STATES = [s.value for s in WorkItemStatus]


def _get_connection():
    return psycopg2.connect(_DSN)


@dataclasses.dataclass(frozen=True)
class WorkItemRow:
    work_item_id: str
    tenant_id: str
    source: str
    status: str            # estado interno §9.3
    public_status: str     # projeção grosseira (running/blocked/done/failed)
    repo: str | None
    pr_number: int | None
    requester: str
    risk_class: str | None
    quarantined: bool
    updated_at: Any


@dataclasses.dataclass(frozen=True)
class TenantBudget:
    tenant_id: str
    monthly_budget_usd: Decimal
    spent_usd: Decimal
    remaining_usd: Decimal
    max_concurrent_work_items: int
    active_work_items: int
    kill_switch_enabled: bool


def _row_to_work_item(row) -> WorkItemRow:
    try:
        internal = WorkItemStatus(row["status"])
        public = to_public_status(internal)
    except ValueError:
        public = "running"
    return WorkItemRow(
        work_item_id=row["id"],
        tenant_id=row["tenant_id"],
        source=row["source"],
        status=row["status"],
        public_status=public,
        repo=row["repo"],
        pr_number=row["pr_number"],
        requester=row["requester"],
        risk_class=row["risk_class"],
        quarantined=bool(row["quarantined"]),
        updated_at=row["updated_at"],
    )


def list_work_items(*, tenant_id: str | None = None, conn=None) -> list[WorkItemRow]:
    """Todos os work items (ou de um tenant), com flag de quarentena (join com
    dse_work_item_quarantine ativa)."""
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            base = """
                SELECT w.id, w.tenant_id, w.source, w.status, w.repo, w.pr_number,
                       w.requester, w.risk_class, w.updated_at,
                       (q.work_item_id IS NOT NULL) AS quarantined
                FROM work_items w
                LEFT JOIN dse_work_item_quarantine q
                       ON q.work_item_id = w.id AND q.released_at IS NULL
            """
            if tenant_id is not None:
                cur.execute(base + " WHERE w.tenant_id = %s ORDER BY w.updated_at DESC", (tenant_id,))
            else:
                cur.execute(base + " ORDER BY w.tenant_id, w.updated_at DESC")
            rows = cur.fetchall()
    finally:
        if owns:
            conn.close()
    return [_row_to_work_item(r) for r in rows]


def work_items_by_state(*, tenant_id: str | None = None, conn=None) -> dict[str, list[WorkItemRow]]:
    """Agrupa por estado interno §9.3. Todos os estados aparecem como chave
    (mesmo vazios) para o board mostrar a máquina inteira."""
    grouped: dict[str, list[WorkItemRow]] = {s: [] for s in ALL_STATES}
    for wi in list_work_items(tenant_id=tenant_id, conn=conn):
        grouped.setdefault(wi.status, []).append(wi)
    return grouped


def list_tenants(conn=None) -> list[str]:
    """União dos tenants vistos em work_items e em tenant_config."""
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT tenant_id FROM work_items
                UNION
                SELECT tenant_id FROM tenant_config
                ORDER BY 1
                """
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        if owns:
            conn.close()


def get_tenant_budget(tenant_id: str, conn=None) -> TenantBudget:
    """Budget + custo corrente. Custo agregado das linhas de audit que carregam
    `details->>'cost_usd'` (ex.: `coder_turn_completed`) — a mesma fonte que o
    OTel collector consome; é uma aproximação honesta enquanto o custo real de
    provider não é gravado (ver README, gaps). `active` = work items em estados
    não-terminais."""
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT monthly_budget_usd, max_concurrent_work_items, kill_switch_enabled
                FROM tenant_config WHERE tenant_id = %s
                """,
                (tenant_id,),
            )
            cfg = cur.fetchone()
            budget = Decimal(cfg[0]) if cfg else Decimal("0")
            max_conc = cfg[1] if cfg else 0
            kill = cfg[2] if cfg else False

            cur.execute(
                """
                SELECT COALESCE(SUM((details->>'cost_usd')::numeric), 0)
                FROM audit_log
                WHERE tenant_id = %s AND details ? 'cost_usd'
                """,
                (tenant_id,),
            )
            spent = Decimal(cur.fetchone()[0])

            cur.execute(
                """
                SELECT COUNT(*) FROM work_items
                WHERE tenant_id = %s AND status NOT IN ('done','failed','escalated','blocked')
                """,
                (tenant_id,),
            )
            active = cur.fetchone()[0]
    finally:
        if owns:
            conn.close()
    return TenantBudget(
        tenant_id=tenant_id,
        monthly_budget_usd=budget,
        spent_usd=spent,
        remaining_usd=budget - spent,
        max_concurrent_work_items=max_conc,
        active_work_items=active,
        kill_switch_enabled=kill,
    )


def get_audit_trail(work_item_id: str, *, limit: int = 200, conn=None) -> list[dict[str, Any]]:
    """Trilha de audit de um work item (ordem cronológica)."""
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                """
                SELECT ts, actor, action, details
                FROM audit_log WHERE work_item_id = %s
                ORDER BY ts ASC, id ASC LIMIT %s
                """,
                (work_item_id, limit),
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        if owns:
            conn.close()
