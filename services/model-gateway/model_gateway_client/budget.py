"""Call-time budget enforcement (WSD-E2-T2).

Two caps checked on EVERY call, before letting the call through:
  - the WorkItem's runtime budget (`work_item_budgets`, or `per_task_usd` from
    WS-F's access bundle);
  - the tenant's AGGREGATE budget for the month (`tenant_config.monthly_budget_usd`
    from WS-F, or `monthly_usd` from the access bundle).

"spent-so-far" comes from the DURABLE ledger (`ledger.py` / `model_call_ledger`)
— not from an in-memory counter — so it survives a restart and aggregates
across processes.

P6 (decline-never-truncate): exhaustion -> CLEAN refusal at the call boundary
(a typed exception that `gateway_call` converts into a `GatewayCallError` with
a `GatewayErrorResponse{error="budget_exhausted"}` body; the WS-B workflow
turns that into Failed). We never cut off mid-generation.

Cap semantics: check spent-so-far BEFORE the call. If it already reached/passed
the cap, refuse the NEXT call (the real cost is only known after the response;
refusing at the next call's boundary is the correct "decline at boundary"
behavior). With no cap resolved (none of the sources defines a limit) -> no
enforcement (Phase 1 behavior preserved for tenants with no config).
"""
from __future__ import annotations

from dataclasses import dataclass

from . import db, ledger


@dataclass(frozen=True)
class BudgetCaps:
    work_item_cap_usd: float | None
    tenant_monthly_cap_usd: float | None
    work_item_source: str | None
    tenant_source: str | None


@dataclass(frozen=True)
class BudgetStatus:
    work_item_spent_usd: float
    tenant_spent_usd: float
    caps: BudgetCaps

    @property
    def work_item_exhausted(self) -> bool:
        c = self.caps.work_item_cap_usd
        return c is not None and self.work_item_spent_usd >= c

    @property
    def tenant_exhausted(self) -> bool:
        c = self.caps.tenant_monthly_cap_usd
        return c is not None and self.tenant_spent_usd >= c


def _access_bundle_budgets(tenant_id: str) -> dict:
    """`budgets` JSONB from the tenant's DEFAULT bundle (WS-F). Defensive:
    missing table / error -> {} (no cap coming from the bundle)."""
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT budgets FROM dse_access_bundle "
                "WHERE tenant_id = %s AND channel IS NULL LIMIT 1",
                (tenant_id,),
            )
            row = cur.fetchone()
            if row and isinstance(row[0], dict):
                return row[0]
            return {}
    except Exception:
        conn.rollback()
        return {}
    finally:
        conn.close()


def _work_item_cap(work_item_id: str) -> tuple[float | None, str | None]:
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT max_budget_usd FROM work_item_budgets WHERE work_item_id = %s",
                (work_item_id,),
            )
            row = cur.fetchone()
            if row is not None:
                return float(row[0]), "work_item_budgets"
            return None, None
    finally:
        conn.close()


def _tenant_monthly_cap(tenant_id: str) -> tuple[float | None, str | None]:
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT monthly_budget_usd FROM tenant_config WHERE tenant_id = %s",
                (tenant_id,),
            )
            row = cur.fetchone()
            if row is not None and row[0] is not None:
                return float(row[0]), "tenant_config.monthly_budget_usd"
            return None, None
    except Exception:
        conn.rollback()
        return None, None
    finally:
        conn.close()


def resolve_caps(tenant_id: str, work_item_id: str) -> BudgetCaps:
    """Resolves the caps. Authority: WS-F's access bundle first (it is Phase 2's
    administrable budget control), falling back to the simpler tables
    (`work_item_budgets`, `tenant_config`)."""
    bundle = _access_bundle_budgets(tenant_id)

    wi_cap: float | None = None
    wi_src: str | None = None
    if "per_task_usd" in bundle and bundle["per_task_usd"] is not None:
        wi_cap, wi_src = float(bundle["per_task_usd"]), "access_bundle.per_task_usd"
    else:
        wi_cap, wi_src = _work_item_cap(work_item_id)

    t_cap: float | None = None
    t_src: str | None = None
    if "monthly_usd" in bundle and bundle["monthly_usd"] is not None:
        t_cap, t_src = float(bundle["monthly_usd"]), "access_bundle.monthly_usd"
    else:
        t_cap, t_src = _tenant_monthly_cap(tenant_id)

    return BudgetCaps(
        work_item_cap_usd=wi_cap,
        tenant_monthly_cap_usd=t_cap,
        work_item_source=wi_src,
        tenant_source=t_src,
    )


def get_status(tenant_id: str, work_item_id: str) -> BudgetStatus:
    caps = resolve_caps(tenant_id, work_item_id)
    return BudgetStatus(
        work_item_spent_usd=ledger.spent_for_work_item(work_item_id),
        tenant_spent_usd=ledger.spent_for_tenant_this_month(tenant_id),
        caps=caps,
    )


def set_work_item_budget(work_item_id: str, tenant_id: str, max_budget_usd: float) -> None:
    """Operator/orchestrator (WS-B) helper to set a WorkItem's runtime cap.
    Idempotent (upsert)."""
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO work_item_budgets (work_item_id, tenant_id, max_budget_usd)
                VALUES (%s, %s, %s)
                ON CONFLICT (work_item_id)
                DO UPDATE SET max_budget_usd = EXCLUDED.max_budget_usd,
                              tenant_id = EXCLUDED.tenant_id
                """,
                (work_item_id, tenant_id, max_budget_usd),
            )
        conn.commit()
    finally:
        conn.close()
