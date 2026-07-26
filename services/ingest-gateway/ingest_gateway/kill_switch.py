"""Per-(tenant, channel) kill switch — WSA-E1-T3. Consulted by the gateway
BEFORE `admit_work_item`; an event from a disabled channel creates no WorkItem
and is not processed, and emits
`dse_audit.emit(action="admission_blocked_kill_switch")`.

Finer granularity than `tenant_config.kill_switch_enabled` (WS-F, whole
tenant) — we check both: if the whole tenant has the kill switch on (WS-F
table), we treat the channel as disabled too. Reading `tenant_config` is
best-effort/optional (defensive import) because that table is owned by WS-F
and may not exist in every test environment.
"""
from __future__ import annotations

from psycopg2.extensions import connection as _Connection


def is_channel_killed(conn: _Connection, tenant_id: str, channel: str) -> tuple[bool, str | None]:
    """Returns (killed, reason). `channel` is the channel identifier — e.g. the
    Slack channel id, or `owner/repo` for GitHub."""
    with conn.cursor() as cur:
        # 1) tenant-wide kill switch (WS-F), if the table exists in this environment.
        try:
            cur.execute(
                "SELECT kill_switch_enabled, kill_switch_reason FROM tenant_config WHERE tenant_id = %s",
                (tenant_id,),
            )
            row = cur.fetchone()
            if row is not None and row[0]:
                return True, row[1] or "tenant_wide_kill_switch"
        except Exception:
            conn.rollback()  # table may not exist in this test environment; fall through to the channel

        # 2) per-channel kill switch (WS-A, migrations/0002_wsa.sql).
        cur.execute(
            "SELECT active, reason FROM channel_kill_switches WHERE tenant_id = %s AND channel = %s",
            (tenant_id, channel),
        )
        row = cur.fetchone()
        if row is not None and row[0]:
            return True, row[1] or "channel_kill_switch"

    return False, None
