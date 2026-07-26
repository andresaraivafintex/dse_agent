"""Scoped gateway kill switch + in-flight model reassign (WSD-E4-T2).

Kill switch — a 4-scope matrix (global | tenant | work_item | channel):
  - at call time the gateway ENFORCES the scopes visible in the call headers:
    global, tenant, work_item;
  - `channel` is not visible at the gateway (the header does not carry the
    channel) — channel-scoped rows are honored at ADMISSION by WS-A/WS-B; they
    live in this table for unified operability, documented in the README.

"Wires into the WS-B/WS-F controls": the check ALSO reads the other
workstreams' tables, so an operator who trips the kill switch through any of
those paths stops model calls:
  - global : `gateway_kill_switches(scope='global')` OR `dse_kill_switch_global`
             (WS-F, WSF-E6-T2);
  - tenant : `gateway_kill_switches(scope='tenant')` OR
             `tenant_config.kill_switch_enabled` (WS-F);
  - work_item: `gateway_kill_switches(scope='work_item')`.

<60s effect: the check reads with a short TTL cache (default 5s), well under
60s — once tripped, the next call in that scope (within <=TTL s) is already
refused. A kill does NOT interrupt a generation already in flight (there is no
stream here); it zeroes out the EMISSION of new calls in that scope, which is
the requirement.

In-flight model reassign: an operator swaps a WorkItem's effective model; the
next call for that work_item uses `to_model` instead of the requested one. The
reassign does NOT bypass policy (the effective model still goes through
`policy.py`).

Every operator mutation (kill on/off, reassign set/clear) produces an audit row
via `dse_audit.emit` (P8).
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

from dse_audit.client import emit as audit_emit

from . import db

_AUDIT_ACTOR_DEFAULT = "system:model-gateway"
_CACHE_TTL_SECONDS = float(os.environ.get("DSE_CONTROLS_CACHE_TTL_SECONDS", "5"))

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, object]] = {}


def _now() -> float:
    return time.monotonic()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


@dataclass(frozen=True)
class KillDecision:
    scope_type: str
    scope_id: str
    reason: str | None
    source: str  # table/column it came from (audit)


# ---------------------------------------------------------------------------
# Kill switch — read (call time)
# ---------------------------------------------------------------------------
def _load_kill_state() -> dict:
    """Cached snapshot of all relevant kill state (gateway + WS-F). One read
    per TTL avoids N queries per model call."""
    with _cache_lock:
        hit = _cache.get("kill_state")
        if hit is not None and (_now() - hit[0]) < _CACHE_TTL_SECONDS:
            return hit[1]  # type: ignore[return-value]

    state = {
        "gateway": {},  # (scope_type, scope_id) -> reason
        "global_wsf": None,  # reason or None
        "tenant_wsf": {},  # tenant_id -> reason
    }
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT scope_type, scope_id, reason FROM gateway_kill_switches WHERE enabled"
            )
            for scope_type, scope_id, reason in cur.fetchall():
                state["gateway"][(scope_type, scope_id)] = reason

        # WS-F: global kill switch (defensive — the table may not exist yet).
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "SELECT reason FROM dse_kill_switch_global WHERE id = 'global' AND enabled"
                )
                row = cur.fetchone()
                if row is not None:
                    state["global_wsf"] = row[0] or "global kill switch (WS-F)"
            except Exception:
                conn.rollback()

        # WS-F: per-tenant kill switch (tenant_config).
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "SELECT tenant_id, kill_switch_reason FROM tenant_config WHERE kill_switch_enabled"
                )
                for tenant_id, reason in cur.fetchall():
                    state["tenant_wsf"][tenant_id] = reason or "tenant kill switch (WS-F)"
            except Exception:
                conn.rollback()
    finally:
        conn.close()

    with _cache_lock:
        _cache["kill_state"] = (_now(), state)
    return state


def is_killed(tenant_id: str, work_item_id: str) -> KillDecision | None:
    """Returns the first applicable kill decision (or None). Order: global,
    tenant, work_item — broadest first."""
    st = _load_kill_state()

    # global
    if ("global", "*") in st["gateway"]:
        return KillDecision("global", "*", st["gateway"][("global", "*")], "gateway_kill_switches")
    if st["global_wsf"] is not None:
        return KillDecision("global", "*", st["global_wsf"], "dse_kill_switch_global")

    # tenant
    if ("tenant", tenant_id) in st["gateway"]:
        return KillDecision(
            "tenant", tenant_id, st["gateway"][("tenant", tenant_id)], "gateway_kill_switches"
        )
    if tenant_id in st["tenant_wsf"]:
        return KillDecision("tenant", tenant_id, st["tenant_wsf"][tenant_id], "tenant_config")

    # work_item
    if ("work_item", work_item_id) in st["gateway"]:
        return KillDecision(
            "work_item",
            work_item_id,
            st["gateway"][("work_item", work_item_id)],
            "gateway_kill_switches",
        )
    return None


# ---------------------------------------------------------------------------
# Kill switch — mutation (WS-B/WS-F operator)
# ---------------------------------------------------------------------------
def set_kill_switch(
    scope_type: str,
    scope_id: str = "*",
    *,
    enabled: bool = True,
    reason: str | None = None,
    actor: str = _AUDIT_ACTOR_DEFAULT,
    tenant_id: str | None = None,
) -> None:
    """Turns the gateway kill switch on/off for a scope. Idempotent. Emits
    audit. `scope_type` in {global, tenant, work_item, channel}."""
    if scope_type not in ("global", "tenant", "work_item", "channel"):
        raise ValueError(f"invalid scope_type: {scope_type}")
    if scope_type == "global":
        scope_id = "*"

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO gateway_kill_switches (scope_type, scope_id, enabled, reason, actor)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (scope_type, scope_id)
                DO UPDATE SET enabled = EXCLUDED.enabled,
                              reason = EXCLUDED.reason,
                              actor = EXCLUDED.actor
                """,
                (scope_type, scope_id, enabled, reason, actor),
            )
        conn.commit()
    finally:
        conn.close()

    clear_cache()
    audit_emit(
        actor=actor,
        action="gateway.kill_switch_set" if enabled else "gateway.kill_switch_cleared",
        tenant_id=tenant_id or (scope_id if scope_type == "tenant" else "*"),
        work_item_id=scope_id if scope_type == "work_item" else None,
        details={"scope_type": scope_type, "scope_id": scope_id, "enabled": enabled, "reason": reason},
    )


# ---------------------------------------------------------------------------
# In-flight model reassign
# ---------------------------------------------------------------------------
def resolve_reassignment(work_item_id: str) -> str | None:
    """The model the work_item was reassigned to (or None). Cached with a short
    TTL (same <60s window as the kill switch)."""
    key = f"reassign:{work_item_id}"
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and (_now() - hit[0]) < _CACHE_TTL_SECONDS:
            return hit[1]  # type: ignore[return-value]

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT to_model FROM model_reassignments WHERE work_item_id = %s AND is_active",
                (work_item_id,),
            )
            row = cur.fetchone()
            to_model = row[0] if row else None
    finally:
        conn.close()

    with _cache_lock:
        _cache[key] = (_now(), to_model)
    return to_model


def reassign_model(
    work_item_id: str,
    to_model: str,
    *,
    reason: str | None = None,
    actor: str = _AUDIT_ACTOR_DEFAULT,
    tenant_id: str = "*",
) -> None:
    """Reassigns a WorkItem's effective model in flight. Deactivates any
    previously active reassignment and creates the new one (at most one active
    per work_item). Emits audit."""
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE model_reassignments SET is_active = false "
                "WHERE work_item_id = %s AND is_active",
                (work_item_id,),
            )
            cur.execute(
                """
                INSERT INTO model_reassignments (work_item_id, to_model, reason, actor, is_active)
                VALUES (%s, %s, %s, %s, true)
                """,
                (work_item_id, to_model, reason, actor),
            )
        conn.commit()
    finally:
        conn.close()

    clear_cache()
    audit_emit(
        actor=actor,
        action="gateway.model_reassigned",
        tenant_id=tenant_id,
        work_item_id=work_item_id,
        details={"to_model": to_model, "reason": reason},
    )


def clear_reassignment(
    work_item_id: str, *, actor: str = _AUDIT_ACTOR_DEFAULT, tenant_id: str = "*"
) -> None:
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE model_reassignments SET is_active = false "
                "WHERE work_item_id = %s AND is_active",
                (work_item_id,),
            )
        conn.commit()
    finally:
        conn.close()

    clear_cache()
    audit_emit(
        actor=actor,
        action="gateway.model_reassignment_cleared",
        tenant_id=tenant_id,
        work_item_id=work_item_id,
        details={},
    )
