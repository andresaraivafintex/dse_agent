"""Read queries over the audit ledger (WSF-E1-T2 — the WS-F extension on top of
the foundation package `dse_audit`, per CONVENTIONS.md: "packages/dse_audit/ |
Foundation (minimum) -> WS-F extends [...] per-WorkItem reconstruction queries
(dse_audit.queries)").

This module is purely additive: it does not touch `dse_audit.client` (`emit`
remains the only write path). Two functions cover the Phase 1 exit criterion
("first audit-based reconstruction exercise passes"):

  - `reconstruct_work_item_history(work_item_id)`: a WorkItem's full timeline via
    a single SELECT ordered by `ts`.
  - `export_audit_range(tenant_id, start, end)`: compliance-grade export (list of
    dicts, CSV-serializable) of every audit row for a tenant over a time range —
    for external auditors/regulators.

P8 (evidence over assertion): these are the only two supported ways to "prove"
what happened to a WorkItem — never reconstruct from application logs, always
from audit_log.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
from typing import Any

from .client import get_connection


def reconstruct_work_item_history(work_item_id: str, conn=None) -> list[dict[str, Any]]:
    """Reconstructs a WorkItem's full timeline from ONE single SELECT on
    `audit_log`, ordered by `ts` (with `id` as a stable tiebreaker when two
    events share the same timestamp).

    Returns a list of dicts in the chronological order the events occurred:
    [{ts, actor, action, details, tenant_id}, ...]. Each dict represents "who did
    what, when" — the audit-based reconstruction exercise required by the Phase 1
    exit criterion.

    Makes no assumption about which actions exist (admitted, clarified, plan,
    implementing, l1_passed, pr_opened, review_approved, merged, ...) — it is
    agnostic to the action vocabulary and just returns what was recorded.
    """
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ts, tenant_id, actor, action, details
                FROM audit_log
                WHERE work_item_id = %s
                ORDER BY ts ASC, id ASC
                """,
                (work_item_id,),
            )
            rows = cur.fetchall()
    finally:
        if owns_conn:
            conn.close()

    return [
        {
            "ts": row[0],
            "tenant_id": row[1],
            "actor": row[2],
            "action": row[3],
            "details": row[4],
        }
        for row in rows
    ]


def export_audit_range(
    tenant_id: str,
    start: dt.datetime,
    end: dt.datetime,
    conn=None,
) -> list[dict[str, Any]]:
    """Compliance-grade export of every audit row for a tenant over the range
    [start, end) — produced on demand for an external auditor/regulator
    (NFR-03). A single SELECT, ordered by `ts`.

    `start`/`end` must be timezone-aware (UTC recommended); the comparison runs
    directly against the `ts` column (TIMESTAMPTZ).
    """
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ts, tenant_id, work_item_id, actor, action, details
                FROM audit_log
                WHERE tenant_id = %s AND ts >= %s AND ts < %s
                ORDER BY ts ASC, id ASC
                """,
                (tenant_id, start, end),
            )
            rows = cur.fetchall()
    finally:
        if owns_conn:
            conn.close()

    return [
        {
            "ts": row[0],
            "tenant_id": row[1],
            "work_item_id": row[2],
            "actor": row[3],
            "action": row[4],
            "details": row[5],
        }
        for row in rows
    ]


def export_audit_range_csv(tenant_id: str, start: dt.datetime, end: dt.datetime, conn=None) -> str:
    """Same export as `export_audit_range`, serialized as a CSV string ready to
    attach to an audit report. `details` (JSONB) is serialized as compact JSON
    text in the matching column.
    """
    import json

    rows = export_audit_range(tenant_id, start, end, conn=conn)
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf, fieldnames=["ts", "tenant_id", "work_item_id", "actor", "action", "details"]
    )
    writer.writeheader()
    for row in rows:
        out = dict(row)
        out["ts"] = row["ts"].isoformat() if isinstance(row["ts"], dt.datetime) else row["ts"]
        out["details"] = json.dumps(row["details"], sort_keys=True)
        writer.writerow(out)
    return buf.getvalue()
