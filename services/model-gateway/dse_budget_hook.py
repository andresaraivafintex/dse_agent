"""Plan 08 §F (F2) + plan 09 Phase 3 — LiteLLM proxy pre-call hook:
SERVER-SIDE (non-bypassable) budget + kill-switch enforcement, FAIL-CLOSED with
bounded degradation.

`model_gateway_client`'s `enforce_call` runs on the CLIENT — a caller talking
straight to the proxy (with the master key / a virtual key) would bypass it.
This hook runs INSIDE the LiteLLM proxy (`litellm_settings.callbacks`), so
EVERY call goes through it, wherever it comes from. It is defense in depth: the
client keeps checking (fail-fast/UX), the proxy is the authority.

Deliberately self-contained (only psycopg2, which the LiteLLM image already
ships): so it can be dropped into the stock image without installing
`model_gateway_client`. It MIRRORS the logic of `controls.is_killed` +
`budget.resolve_caps`/ledger — if one changes, change both (documented).

Context (tenant/work_item) comes from the headers the client already sends:
`X-Dse-Tenant-Id` / `X-Dse-Work-Item-Id`. Without those headers the call is not
from the DSE orchestrator — let it through (virtual keys already scope it by
model/budget).

Posture on Postgres failure (Phase 3 — it used to be a silent fail-OPEN):
  1. Every call queries the DB (with a short connect_timeout — a HUNG Postgres
     no longer stalls the pre-call) and the most recent good verdict per
     (tenant, work_item) is cached in-process.
  2. DB unreachable + cached verdict within the HARD TTL -> serve the cache,
     LOGGED as a DEGRADED decision (visible, counted, with an expiry).
  3. DB unreachable + cache missing/expired -> BLOCK the DSE call with a
     retryable 503 ("budget_enforcement_unavailable") — a real fail-closed.
     Calls with NO DSE context are never blocked by unavailability (virtual
     keys already scope them; the max_budget cap remains as a backstop).
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import time
from typing import Any, Callable

logger = logging.getLogger("dse_budget_hook")

_DSN = os.environ.get("DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse")

# Degradation knobs (Phase 3). The short CONNECT_TIMEOUT is what stops a hung
# Postgres from holding up the proxy's event loop.
CONNECT_TIMEOUT_S = int(os.environ.get("DSE_BUDGET_HOOK_CONNECT_TIMEOUT_S", "3"))
HARD_TTL_S = float(os.environ.get("DSE_BUDGET_HOOK_HARD_TTL_S", "600"))

# Observability counters (exposed for tests and for log scraping): every
# decision off the happy path is COUNTED and LOGGED — silent fail-open no
# longer exists.
DEGRADED_DECISIONS = 0          # served a cached verdict with the DB down
UNAVAILABLE_BLOCKS = 0          # blocked due to unavailability with no cache

# (tenant_id, work_item_id) -> ((allowed, error, reason), monotonic_ts)
_VERDICT_CACHE: dict[tuple[str, str], tuple[tuple[bool, str, str], float]] = {}


def _conn():
    import psycopg2  # present in the LiteLLM image (Postgres support)
    return psycopg2.connect(_DSN, connect_timeout=CONNECT_TIMEOUT_S)


# --- kill-switch (mirrors controls.is_killed) --------------------------------

def _kill_reason(cur, tenant_id: str, work_item_id: str) -> str | None:
    cur.execute(
        "SELECT scope_type, scope_id, reason FROM gateway_kill_switches WHERE enabled"
    )
    for scope_type, scope_id, reason in cur.fetchall():
        if scope_type == "global":
            return reason or "global_kill_switch"
        if scope_type == "tenant" and scope_id == tenant_id:
            return reason or "tenant_kill_switch"
        if scope_type == "work_item" and scope_id == work_item_id:
            return reason or "work_item_kill_switch"
    # WS-F sources
    cur.execute("SELECT reason FROM dse_kill_switch_global WHERE id='global' AND enabled")
    row = cur.fetchone()
    if row:
        return row[0] or "global_kill_switch"
    cur.execute(
        "SELECT kill_switch_reason FROM tenant_config WHERE tenant_id=%s AND kill_switch_enabled",
        (tenant_id,),
    )
    row = cur.fetchone()
    if row:
        return row[0] or "tenant_kill_switch"
    return None


# --- budget (mirrors budget.resolve_caps + ledger spent) ---------------------

def _one(cur, sql: str, params: tuple) -> Any:
    cur.execute(sql, params)
    row = cur.fetchone()
    return row[0] if row and row[0] is not None else None


def _budget_exhausted(cur, tenant_id: str, work_item_id: str) -> str | None:
    bundle = _one(
        cur, "SELECT budgets FROM dse_access_bundle WHERE tenant_id=%s AND channel IS NULL LIMIT 1",
        (tenant_id,),
    ) or {}
    if not isinstance(bundle, dict):
        bundle = {}
    wi_cap = bundle.get("per_task_usd")
    if wi_cap is None:
        wi_cap = _one(cur, "SELECT max_budget_usd FROM work_item_budgets WHERE work_item_id=%s", (work_item_id,))
    t_cap = bundle.get("monthly_usd")
    if t_cap is None:
        t_cap = _one(cur, "SELECT monthly_budget_usd FROM tenant_config WHERE tenant_id=%s", (tenant_id,))

    if wi_cap is not None:
        spent = _one(cur, "SELECT COALESCE(sum(cost_usd),0) FROM model_call_ledger WHERE work_item_id=%s", (work_item_id,)) or 0
        if float(spent) >= float(wi_cap):
            return f"work_item_budget_exhausted({spent:.4f}/{float(wi_cap):.2f})"
    if t_cap is not None:
        month_start = _dt.datetime.now(_dt.timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        spent = _one(
            cur,
            "SELECT COALESCE(sum(cost_usd),0) FROM model_call_ledger WHERE tenant_id=%s AND created_at >= %s",
            (tenant_id, month_start),
        ) or 0
        if float(spent) >= float(t_cap):
            return f"tenant_monthly_budget_exhausted({spent:.4f}/{float(t_cap):.2f})"
    return None


def _decide(cur, tenant_id: str, work_item_id: str) -> tuple[bool, str, str]:
    reason = _kill_reason(cur, tenant_id, work_item_id)
    if reason:
        return False, "kill_switch_active", reason
    reason = _budget_exhausted(cur, tenant_id, work_item_id)
    if reason:
        return False, "budget_exhausted", reason
    return True, "", "ok"


def _degraded_verdict(
    key: tuple[str, str], now_ts: float, exc: Exception
) -> tuple[bool, str, str]:
    """Postgres unreachable: serve the last GOOD verdict within the hard TTL
    (visible degradation) or block (fail-closed). Never a blind fail-open."""
    global DEGRADED_DECISIONS, UNAVAILABLE_BLOCKS
    cached = _VERDICT_CACHE.get(key)
    if cached is not None:
        verdict, at = cached
        age = now_ts - at
        if age <= HARD_TTL_S:
            DEGRADED_DECISIONS += 1
            logger.warning(
                "dse_budget_hook DEGRADED: postgres unreachable (%s: %s); serving a cached "
                "verdict %.0fs old for %s (hard TTL %.0fs)",
                type(exc).__name__, str(exc)[:120], age, key, HARD_TTL_S,
            )
            return verdict
    UNAVAILABLE_BLOCKS += 1
    logger.error(
        "dse_budget_hook UNAVAILABLE: postgres unreachable (%s: %s) and no fresh verdict "
        "for %s — DSE call BLOCKED (fail-closed)",
        type(exc).__name__, str(exc)[:120], key,
    )
    return (
        False,
        "budget_enforcement_unavailable",
        f"postgres unreachable and no fresh verdict (last: "
        f"{'none' if cached is None else f'{now_ts - cached[1]:.0f}s ago'})",
    )


def evaluate_gate(
    tenant_id: str | None,
    work_item_id: str | None,
    *,
    connect: Callable[[], Any] | None = None,
    now: Callable[[], float] | None = None,
) -> tuple[bool, str, str]:
    """Pure, testable core (no litellm): returns (allowed, error, reason). With
    no DSE context -> allowed (not an orchestrator call). `connect`/`now` are
    dependency injection for the degradation-cell tests."""
    if not tenant_id or not work_item_id:
        return True, "", "no_dse_context"
    key = (tenant_id, work_item_id)
    now_ts = (now or time.monotonic)()
    try:
        conn = (connect or _conn)()
    except Exception as exc:  # noqa: BLE001 — the "DB down" cell
        return _degraded_verdict(key, now_ts, exc)
    try:
        with conn.cursor() as cur:
            verdict = _decide(cur, tenant_id, work_item_id)
    except Exception as exc:  # noqa: BLE001 — the query failed mid-flight
        return _degraded_verdict(key, now_ts, exc)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    _VERDICT_CACHE[key] = (verdict, now_ts)
    return verdict


# --- CustomLogger wrapper (litellm import guarded for testability) -----------

try:
    from litellm.integrations.custom_logger import CustomLogger as _Base
except Exception:  # pragma: no cover — dev/test venv without litellm
    class _Base:  # type: ignore
        pass


class DseBudgetKillSwitchHook(_Base):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):  # noqa: ANN001
        headers = (data.get("proxy_server_request") or {}).get("headers") or {}
        tenant_id = headers.get("x-dse-tenant-id")
        work_item_id = headers.get("x-dse-work-item-id")
        allowed, error, reason = evaluate_gate(tenant_id, work_item_id)
        if allowed:
            return data
        _audit(tenant_id, work_item_id, error, reason)
        from fastapi import HTTPException
        # 403 kill-switch / 402 budget: policy refusals (P6, non-retryable on
        # the client). 503 enforcement unavailability: RETRYABLE — the Postgres
        # blip passes and the next call decides for real.
        status = {"kill_switch_active": 403, "budget_exhausted": 402}.get(error, 503)
        raise HTTPException(status_code=status, detail={"error": error, "reason": reason,
                                                        "enforced_by": "dse_budget_hook"})


def _audit(tenant_id, work_item_id, error, reason) -> None:  # best-effort (P8)
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO audit_log (work_item_id, tenant_id, actor, action, details) "
                "VALUES (%s,%s,'system:model-gateway-proxy',%s,%s::jsonb)",
                (work_item_id, tenant_id, f"proxy_precall_{error}",
                 __import__("json").dumps({"reason": reason})),
            )
        conn.commit()
        conn.close()
    except Exception:  # pragma: no cover
        pass


proxy_handler_instance = DseBudgetKillSwitchHook()
