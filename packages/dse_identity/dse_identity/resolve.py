"""Identity map foundations (WSF-E3-T1). Fase 1: resolution by self-registration
on first appearance — NO SSO/SCIM (that is ADR-22, closed, plus WSF-E3-T3, in
Fase 2). Every adapter calls `resolve_principal` before writing `actor` into any
`ConversationEvent`, `WorkItem.requester` or audit row.

Stable contract (WS-A depends on it in Fase 1; WS-F swaps the implementation
underneath in Fase 2 without breaking the signature — see WSA-E6-T2b).
"""
from __future__ import annotations

import os
import uuid

import psycopg2

_DSN = os.environ.get(
    "DSE_IDENTITY_DATABASE_URL",
    os.environ.get("DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"),
)


def resolve_principal(platform: str, platform_user_id: str, display_name: str | None = None) -> str:
    """Returns the unique principal_id for (platform, platform_user_id), creating
    it on first appearance. Idempotent: repeated calls with the same
    platform/platform_user_id pair always return the same principal_id.
    """
    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT principal_id FROM identity_links WHERE platform = %s AND platform_user_id = %s",
                (platform, platform_user_id),
            )
            row = cur.fetchone()
            if row is not None:
                return row[0]

            principal_id = f"usr_{uuid.uuid4().hex[:16]}"
            cur.execute(
                "INSERT INTO principals (id, display_name) VALUES (%s, %s)",
                (principal_id, display_name),
            )
            cur.execute(
                "INSERT INTO identity_links (principal_id, platform, platform_user_id) "
                "VALUES (%s, %s, %s) ON CONFLICT (platform, platform_user_id) DO NOTHING",
                (principal_id, platform, platform_user_id),
            )
        conn.commit()

        # rare race: another process created it between the SELECT and the
        # INSERT — the ON CONFLICT above does not overwrite; re-read to pick up
        # the winning principal_id (guarantees 1 principal per platform_user_id).
        with conn.cursor() as cur:
            cur.execute(
                "SELECT principal_id FROM identity_links WHERE platform = %s AND platform_user_id = %s",
                (platform, platform_user_id),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()
