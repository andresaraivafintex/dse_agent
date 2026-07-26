"""WSF-E3-T3 (part of offboarding) — resolves "who may steer a task" by
combining WS-A's explicit allowlist (`tenant_steering_allowlist`) with the
console offboarding state (`dse_console_identity.active`).

WS-A (WSA-E6-T2a) already treats the allowlist as the source of truth for
steering authorization ("no row = not authorized"). This helper adds the ADR-22
offboarding layer: an offboarded principal (`active = false`) may NOT steer even
if it still has a row in the allowlist — offboarding is the overriding
authority. Purely deterministic (P1); denies by default.
"""
from __future__ import annotations

import os

import psycopg2

from .sso import is_console_active

_DSN = os.environ.get(
    "DSE_PLATFORM_DATABASE_URL",
    os.environ.get("DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"),
)


def _get_connection():
    return psycopg2.connect(_DSN)


def is_steering_allowed(tenant_id: str, principal_id: str, conn=None) -> bool:
    """True iff the principal is in the tenant's steering allowlist AND is not
    offboarded in the console. A principal that never logged into the console
    (no row in dse_console_identity) is NOT blocked for that reason — it is only
    blocked when EXPLICITLY deactivated/expired (same rule as
    access_bundles.resolve_plan_approvers). Denies by default."""
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM tenant_steering_allowlist WHERE tenant_id = %s AND principal_id = %s",
                (tenant_id, principal_id),
            )
            in_allowlist = cur.fetchone() is not None
        if not in_allowlist:
            return False
        # has a row in console_identity and is deactivated? block.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM dse_console_identity WHERE principal_id = %s",
                (principal_id,),
            )
            has_console = cur.fetchone() is not None
        if has_console and not is_console_active(principal_id, conn=conn):
            return False
        return True
    finally:
        if owns:
            conn.close()
