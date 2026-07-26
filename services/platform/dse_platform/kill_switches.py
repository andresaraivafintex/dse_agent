"""WSF-E6-T2 — kill switches across the 4 scopes + work item quarantine.

Scopes (broadest to narrowest):
  1. GLOBAL  — `dse_kill_switch_global` (this WS). Blocks all admission across
               every tenant.
  2. TENANT  — `tenant_config.kill_switch_enabled` (dse_platform.tenant_config).
  3. CHANNEL — `channel_kill_switches` (a WS-A table; we write rows into it in
               the same database — data plane, we do not edit WS-A's
               file/migration). Blocks admission for one specific
               (tenant, channel).
  4. TASK    — durable quarantine of a work item (`dse_work_item_quarantine`) +
               (in operator.py) the Temporal `pause`/`cancel` signal. The durable
               flag survives a worker restart and feeds the queue board
               projection.

`is_admission_blocked(tenant, channel)` is the composite that the ingest-gateway
(WS-A) and the model-gateway (WS-D) must consult: it checks global -> tenant ->
channel, in that order, and returns the first block found (or None). Every change
to any switch writes an audit row (P8).
"""
from __future__ import annotations

import dataclasses
import os

import psycopg2
import psycopg2.extras
from dse_audit import emit

from .tenant_config import get_tenant_config, set_kill_switch as _set_tenant_kill_switch

_DSN = os.environ.get(
    "DSE_PLATFORM_DATABASE_URL",
    os.environ.get("DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"),
)


def _get_connection():
    return psycopg2.connect(_DSN)


@dataclasses.dataclass(frozen=True)
class AdmissionBlock:
    scope: str  # "global" | "tenant" | "channel"
    reason: str | None


# ---------------------------------------------------------------------------
# 1. GLOBAL
# ---------------------------------------------------------------------------
def set_global_kill_switch(*, enabled: bool, reason: str | None, actor: str, conn=None) -> None:
    if enabled and not reason:
        raise ValueError("the global kill switch requires `reason` when ACTIVATING (P8)")
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO dse_kill_switch_global (id, enabled, reason)
                VALUES ('global', %s, %s)
                ON CONFLICT (id) DO UPDATE SET enabled = EXCLUDED.enabled, reason = EXCLUDED.reason
                """,
                (enabled, reason),
            )
        emit(
            actor=actor,
            action="global_kill_switch_enabled" if enabled else "global_kill_switch_disabled",
            tenant_id="platform",
            details={"reason": reason},
            conn=conn,
        )
        if owns:
            conn.commit()
    except Exception:
        if owns:
            conn.rollback()
        raise
    finally:
        if owns:
            conn.close()


def get_global_kill_switch(conn=None) -> tuple[bool, str | None]:
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT enabled, reason FROM dse_kill_switch_global WHERE id = 'global'")
            row = cur.fetchone()
    finally:
        if owns:
            conn.close()
    if row is None:
        return (False, None)
    return (row[0], row[1])


# ---------------------------------------------------------------------------
# 2. TENANT (delegates to the tenant_config module, which already audits)
# ---------------------------------------------------------------------------
def set_tenant_kill_switch(tenant_id: str, *, enabled: bool, reason: str | None, actor: str, conn=None):
    return _set_tenant_kill_switch(tenant_id, enabled=enabled, reason=reason, actor=actor, conn=conn)


# ---------------------------------------------------------------------------
# 3. CHANNEL (WS-A's channel_kill_switches table — data plane)
# ---------------------------------------------------------------------------
def set_channel_kill_switch(
    tenant_id: str, channel: str, *, active: bool, reason: str | None, actor: str, conn=None
) -> None:
    """Turns the kill switch of a (tenant, channel) on/off. `active = true` =
    channel OFF (blocks admission) — same semantics as the `active` column of
    WS-A's table."""
    if active and not reason:
        raise ValueError("the channel kill switch requires `reason` when ACTIVATING (P8)")
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO channel_kill_switches (tenant_id, channel, active, reason)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (tenant_id, channel) DO UPDATE SET active = EXCLUDED.active, reason = EXCLUDED.reason
                """,
                (tenant_id, channel, active, reason),
            )
        emit(
            actor=actor,
            action="channel_kill_switch_enabled" if active else "channel_kill_switch_disabled",
            tenant_id=tenant_id,
            details={"channel": channel, "reason": reason},
            conn=conn,
        )
        if owns:
            conn.commit()
    except Exception:
        if owns:
            conn.rollback()
        raise
    finally:
        if owns:
            conn.close()


# ---------------------------------------------------------------------------
# Admission composite (global -> tenant -> channel)
# ---------------------------------------------------------------------------
def is_admission_blocked(tenant_id: str, channel: str | None = None, conn=None) -> AdmissionBlock | None:
    """Returns the first block (broadest first) or None if admissible. Must be
    called by the ingest-gateway (WS-A) and the model-gateway (WS-D) BEFORE
    admitting/running work. Purely deterministic (P1)."""
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        g_enabled, g_reason = get_global_kill_switch(conn=conn)
        if g_enabled:
            return AdmissionBlock(scope="global", reason=g_reason)

        cfg = get_tenant_config(tenant_id, conn=conn)
        if cfg is not None and cfg.kill_switch_enabled:
            return AdmissionBlock(scope="tenant", reason=cfg.kill_switch_reason)

        if channel is not None:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT active, reason FROM channel_kill_switches WHERE tenant_id = %s AND channel = %s",
                    (tenant_id, channel),
                )
                row = cur.fetchone()
            if row is not None and row[0]:
                return AdmissionBlock(scope="channel", reason=row[1])
        return None
    finally:
        if owns:
            conn.close()


# ---------------------------------------------------------------------------
# 4. TASK — durable work item quarantine (counterpart of the signal in operator.py)
# ---------------------------------------------------------------------------
def quarantine_work_item(
    work_item_id: str, tenant_id: str, *, reason: str, actor: str, conn=None
) -> None:
    """Marks a work item as quarantined (durably). Idempotent: re-quarantining an
    already-quarantined item only updates the reason. `reason`/`actor` are
    mandatory (P8)."""
    if not reason:
        raise ValueError("quarantine requires `reason` (P8)")
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO dse_work_item_quarantine (work_item_id, tenant_id, reason, actor)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (work_item_id) DO UPDATE SET
                    reason = EXCLUDED.reason, actor = EXCLUDED.actor,
                    released_at = NULL, released_by = NULL
                """,
                (work_item_id, tenant_id, reason, actor),
            )
        emit(
            actor=actor, action="work_item_quarantined", tenant_id=tenant_id,
            work_item_id=work_item_id, details={"reason": reason}, conn=conn,
        )
        if owns:
            conn.commit()
    except Exception:
        if owns:
            conn.rollback()
        raise
    finally:
        if owns:
            conn.close()


def release_quarantine(work_item_id: str, tenant_id: str, *, actor: str, conn=None) -> None:
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE dse_work_item_quarantine
                SET released_at = now(), released_by = %s
                WHERE work_item_id = %s AND released_at IS NULL
                """,
                (actor, work_item_id),
            )
        emit(
            actor=actor, action="work_item_quarantine_released", tenant_id=tenant_id,
            work_item_id=work_item_id, details={}, conn=conn,
        )
        if owns:
            conn.commit()
    except Exception:
        if owns:
            conn.rollback()
        raise
    finally:
        if owns:
            conn.close()


def is_quarantined(work_item_id: str, conn=None) -> bool:
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM dse_work_item_quarantine WHERE work_item_id = %s AND released_at IS NULL",
                (work_item_id,),
            )
            return cur.fetchone() is not None
    finally:
        if owns:
            conn.close()
