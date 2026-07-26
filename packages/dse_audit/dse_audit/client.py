"""The only write path into the audit ledger (WSF-E1-T1). No other module in the
monorepo may run `INSERT INTO audit_log` directly — import `emit` instead.

Foundation minimum: writes using the `dse_app` role (which has no UPDATE/DELETE
grant — see migrations/0001_foundation.sql). WS-F extends this package in Fase 1
with:
  - per-WorkItem reconstruction queries (`dse_audit.queries`)
  - compliance-grade export per tenant/time range
  - ensuring the tenant's partition exists before the first insert
(see services/platform/README.md for what is still missing).
"""
from __future__ import annotations

import contextlib
import json
import os
from typing import Any

import psycopg2

_DSN = os.environ.get(
    "DSE_AUDIT_DATABASE_URL",
    os.environ.get("DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"),
)


def get_connection():
    return psycopg2.connect(_DSN)


def emit(
    *,
    actor: str,
    action: str,
    tenant_id: str,
    work_item_id: str | None = None,
    details: dict[str, Any] | None = None,
    conn=None,
) -> None:
    """Writes an immutable row into audit_log.

    `actor` is always a resolved principal (via dse_identity) or a
    `system:<component>` string (e.g. `system:ingest-gateway`,
    `system:egress-proxy`) — never a raw platform_user_id (P8: evidence must be
    attributable).

    If `conn` is passed, this joins the caller's transaction (useful when the
    audit event must be atomic with another write); otherwise it opens and closes
    its own connection/transaction.
    """
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit_log (work_item_id, tenant_id, actor, action, details)
                VALUES (%s, %s, %s, %s, %s::jsonb)
                """,
                (work_item_id, tenant_id, actor, action, json.dumps(details or {})),
            )
        if owns_conn:
            conn.commit()
    except Exception:
        if owns_conn:
            with contextlib.suppress(Exception):
                conn.rollback()
        raise
    finally:
        if owns_conn:
            conn.close()
