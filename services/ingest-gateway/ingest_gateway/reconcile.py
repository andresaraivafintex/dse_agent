"""Recovery of human replies that never reached the adapter.

Every surface can lose a reply, each in its own way: a Slack or GitHub webhook
delivery fails while the adapter is down, or the Jira poller's time window moves
past a comment before it is read. The failure is silent by construction — the
human replied, the platform shows their message, and the task simply stays
blocked forever with nobody looking at it. Observed twice on the same afternoon
(BD-40, BD-41), both times needing a hand-written database update to unblock.

The fix is to stop relying solely on delivery: for the handful of work items
that are BLOCKED WAITING ON A HUMAN, re-read their thread and ingest whatever
was missed. Idempotency makes this safe to repeat — a reply already seen dedupes
on `event_id` and costs nothing.

WHAT IS DELIBERATELY NOT RECOVERED
----------------------------------
`awaiting_plan_approval` is excluded, and that exclusion is the point rather
than an oversight.

The adapters keep a TOCTOU defense (WSA-E2-T2): `content_snapshot` is the text
exactly as it arrived in the signed webhook body, and a message is NEVER re-read
afterwards — because an attacker can post something benign, obtain approval, and
edit it afterwards. Re-reading is precisely the operation that defense forbids.

For a clarification answer, re-reading is a reasonable trade: the text becomes
an instruction to an agent, it goes through `sanitize_content` like any other,
and the plan approval gate still stands between it and anything consequential.
For an APPROVAL it is not a trade at all — a recovered approval is a decision
manufactured from text nobody signed. An approval that gets lost stays lost, and
a human re-approves. That is the correct failure mode.
"""
from __future__ import annotations

from typing import Any

# Blocked on a human REPLY — safe to recover, because what comes back is content
# the pipeline already treats as untrusted input.
RECOVERABLE_STATUSES = ("needs_clarification", "awaiting_repo_selection")

# Blocked on a human DECISION — never recovered. See the module docstring.
NON_RECOVERABLE_STATUSES = ("awaiting_plan_approval",)


def recorded_work_item_id(conn, event_id: str) -> str | None:
    """The work item this exact event was already recorded against, or None.

    The recovery sweeps re-read whole threads, so on a task that legitimately
    sits waiting they see the same replies again on every cycle. Recording is
    idempotent, so nothing incorrect happens — but the work leading up to it is
    not free: the steering check runs, `record_signal_event` writes a
    `signal_duplicate_ignored` row, and both land in the audit log.

    Left alone that is not a rounding error. A single work item stuck in
    `needs_clarification` produced ~2,900 audit rows in thirteen hours, the
    audit log is append-only by design, and the console projects it into a
    timeline that becomes almost entirely noise.

    Checking first turns the steady state into one SELECT.

    Returns the ID rather than a bool because a redelivery still has to answer
    the caller with the work item it belongs to. A webhook redelivery is the
    normal case here — the TOCTOU defense relies on the second delivery being
    recognised and answered, not merely dropped — and a bool would have made
    the reply indistinguishable from "no work item".
    """
    with conn.cursor() as cur:
        cur.execute("SELECT work_item_id FROM ingest_events WHERE event_id = %s", (event_id,))
        row = cur.fetchone()
        return row[0] if row else None


def pending_reply_work_items(
    conn, *, tenant_id: str, source: str, limit: int = 50
) -> list[dict[str, Any]]:
    """Work items of `source` that are stuck waiting for a human reply.

    Returns `{work_item_id, source_ref, status}` per row. The set is small by
    nature — a task only sits here between asking a question and being answered
    — so a reconciler may run this on every cycle without thinking about cost.

    `limit` is a blast-radius guard, not a performance one: if something goes
    wrong upstream and thousands of items pile up in a blocked state, a
    reconciler should crawl rather than stampede the platform's API and get the
    whole installation rate-limited.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, source_ref, status FROM work_items "
            "WHERE tenant_id = %s AND source = %s AND status = ANY(%s) "
            "ORDER BY last_transition_at ASC NULLS LAST "
            "LIMIT %s",
            (tenant_id, source, list(RECOVERABLE_STATUSES), limit),
        )
        rows = cur.fetchall()
    return [
        {
            "work_item_id": r[0],
            "source_ref": r[1] if isinstance(r[1], dict) else {},
            "status": r[2],
        }
        for r in rows
    ]
