"""Per-tenant fairness/budget/kill-switch parameters (WS-F's reserved migration,
`migrations/0007_wsf.sql` — table `tenant_config`).

Consumed by the orchestrator (WS-B, before admitting/continuing work for a
tenant) and by the model-gateway (WS-D, before issuing a virtual key) — see
`infra/ALERTING-RULES.md` rule 1 for the budget-exhaustion flow that uses this
module.

P1 (deterministic-or-human): reading/deciding to block a tenant here is purely a
numeric comparison in code — an LLM never decides whether a tenant is over
budget.
P8 (evidence over assertion): every change to `kill_switch_enabled` writes an
audit_log row via `dse_audit.emit` (never silent).
"""
from __future__ import annotations

import dataclasses
import os
from decimal import Decimal
from typing import Any

import psycopg2
import psycopg2.extras
from dse_audit import emit

_DSN = os.environ.get(
    "DSE_PLATFORM_DATABASE_URL",
    os.environ.get("DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"),
)


def _get_connection():
    return psycopg2.connect(_DSN)


@dataclasses.dataclass(frozen=True)
class TenantConfig:
    tenant_id: str
    monthly_budget_usd: Decimal
    max_concurrent_work_items: int
    kill_switch_enabled: bool
    kill_switch_reason: str | None
    fairness: dict[str, Any]


def get_tenant_config(tenant_id: str, conn=None) -> TenantConfig | None:
    """Returns the tenant config, or None if it has not been provisioned yet
    (the caller must treat `None` as "use conservative defaults" — never as
    "no limit")."""
    owns_conn = conn is None
    if owns_conn:
        conn = _get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                """
                SELECT tenant_id, monthly_budget_usd, max_concurrent_work_items,
                       kill_switch_enabled, kill_switch_reason, fairness
                FROM tenant_config WHERE tenant_id = %s
                """,
                (tenant_id,),
            )
            row = cur.fetchone()
    finally:
        if owns_conn:
            conn.close()

    if row is None:
        return None
    return TenantConfig(
        tenant_id=row["tenant_id"],
        monthly_budget_usd=row["monthly_budget_usd"],
        max_concurrent_work_items=row["max_concurrent_work_items"],
        kill_switch_enabled=row["kill_switch_enabled"],
        kill_switch_reason=row["kill_switch_reason"],
        fairness=row["fairness"],
    )


def upsert_tenant_config(
    tenant_id: str,
    *,
    monthly_budget_usd: Decimal | float | None = None,
    max_concurrent_work_items: int | None = None,
    fairness: dict[str, Any] | None = None,
    conn=None,
) -> TenantConfig:
    """Creates the tenant config (with the migration defaults) if it does not
    exist, or updates the fields that were passed (non-None) if it does.
    Idempotent: calling again with the same values changes nothing."""
    owns_conn = conn is None
    if owns_conn:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tenant_config (tenant_id, monthly_budget_usd, max_concurrent_work_items, fairness)
                VALUES (%s, COALESCE(%s, 100.00), COALESCE(%s, 5), COALESCE(%s::jsonb, '{}'::jsonb))
                ON CONFLICT (tenant_id) DO UPDATE SET
                    monthly_budget_usd = COALESCE(EXCLUDED.monthly_budget_usd, tenant_config.monthly_budget_usd),
                    max_concurrent_work_items = COALESCE(EXCLUDED.max_concurrent_work_items, tenant_config.max_concurrent_work_items),
                    fairness = COALESCE(EXCLUDED.fairness, tenant_config.fairness)
                """,
                (
                    tenant_id,
                    monthly_budget_usd,
                    max_concurrent_work_items,
                    None if fairness is None else psycopg2.extras.Json(fairness),
                ),
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

    result = get_tenant_config(tenant_id)
    assert result is not None
    return result


def set_kill_switch(
    tenant_id: str,
    *,
    enabled: bool,
    reason: str | None,
    actor: str,
    conn=None,
) -> TenantConfig:
    """Turns a tenant's kill-switch on/off. Always writes an audit_log row (P8) —
    in the same transaction as the state change, so that audit reconstruction
    (`dse_audit.queries.reconstruct_work_item_history` does not apply here, but
    the atomicity principle is the same as in the rest of the system) never sees
    a kill-switch change without the corresponding event.

    `actor` must be a resolved principal or `system:<component>` — never a raw
    platform_user_id (same rule as `dse_audit.emit`).
    """
    if enabled and not reason:
        raise ValueError("kill_switch_reason is mandatory when ENABLING the kill-switch (P8: never silent)")

    owns_conn = conn is None
    if owns_conn:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tenant_config (tenant_id, kill_switch_enabled, kill_switch_reason)
                VALUES (%s, %s, %s)
                ON CONFLICT (tenant_id) DO UPDATE SET
                    kill_switch_enabled = EXCLUDED.kill_switch_enabled,
                    kill_switch_reason = EXCLUDED.kill_switch_reason
                """,
                (tenant_id, enabled, reason),
            )
        emit(
            actor=actor,
            action="kill_switch_enabled" if enabled else "kill_switch_disabled",
            tenant_id=tenant_id,
            details={"reason": reason},
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

    result = get_tenant_config(tenant_id)
    assert result is not None
    return result
