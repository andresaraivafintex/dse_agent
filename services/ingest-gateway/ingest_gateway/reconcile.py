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

import threading
from typing import Any

# Blocked on a human REPLY — safe to recover, because what comes back is content
# the pipeline already treats as untrusted input.
RECOVERABLE_STATUSES = ("needs_clarification", "awaiting_repo_selection")

# Blocked on a human DECISION — never recovered. See the module docstring.
NON_RECOVERABLE_STATUSES = ("awaiting_plan_approval",)

# Where the last sweep stopped, per (tenant_id, source): the sort key of the
# final row handed out, so the next call resumes AFTER it instead of restarting
# at the oldest item. See `pending_reply_work_items` for why this exists.
_SWEEP_CURSOR: dict[tuple[str, str], tuple[Any, str]] = {}

# The sweep endpoint is an HTTP handler, so two timers (or a human curl) can
# overlap.
#
# WHAT THIS LOCK DOES AND DOES NOT GIVE. It is held only around dict access,
# never across the query — holding it over a database round-trip in an HTTP
# handler would let one slow connection block every other tenant's sweep. So it
# does NOT make a sweep exclusive: two overlapping calls can read the same
# position and serve the same page. What it does give is that a call may only
# replace THE POSITION IT READ (`_store_sweep_cursor`): a slow call whose
# position has since been moved on by a newer sweep, or dropped by a tail wrap,
# loses its write. Without that, the slow call would put the rotation back where
# it began and pages already passed would be re-served indefinitely under load.
#
# Duplicate pages are harmless by design: re-reading a thread is idempotent and
# re-ingestion dedupes on `event_id` (see `recorded_work_item_id`). Skipping is
# the failure that matters, and losing a write can only make a page be re-served,
# never skipped — a page is always the rows immediately after the position it was
# read with, and the position only moves to the end of a page that was handed out.
_SWEEP_CURSOR_LOCK = threading.Lock()

# Sentinels for the row-comparison keyset. `-infinity` starts a rotation (every
# row sorts after it); `infinity` stands for a NULL `last_transition_at`, which
# sorts LAST under `NULLS LAST` and must compare that way in the predicate too.
# Both are passed to Postgres as `%s::timestamptz`, where they are real values.
#
# `_SCAN_START` is also what an ABSENT entry means, so the compare below can
# treat "no cursor" and "at the start" as one value.
_SCAN_START = ("-infinity", "")
_NULL_TRANSITION_KEY = "infinity"


def _store_sweep_cursor(
    key: tuple[str, str], *, expected: tuple[Any, str], new_cursor: tuple[Any, str] | None
) -> None:
    """Move the rotation from `expected` to `new_cursor`, or drop it if None.

    Nothing happens unless the stored position is still the one the caller read.
    An overlapping sweep that started earlier and finished later therefore cannot
    rewind the rotation, and — the hole an earlier version of this file had — it
    cannot re-seat a position that a tail wrap has since dropped either, because
    the wrap goes through this same check instead of popping the key on its own.
    Losing the write is correct: the rows this call served were served, and the
    rotation is at or beyond them.

    Equality, not an ordering, on purpose: a stored key mixes a `datetime` with
    the string sentinels above, `<` between those raises TypeError, and there is
    nothing this function has to decide that ordering them would answer.
    """
    with _SWEEP_CURSOR_LOCK:
        if _SWEEP_CURSOR.get(key, _SCAN_START) != expected:
            return
        if new_cursor is None:
            _SWEEP_CURSOR.pop(key, None)
        else:
            _SWEEP_CURSOR[key] = new_cursor


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


def reset_reply_sweep_cursor(*, tenant_id: str | None = None, source: str | None = None) -> None:
    """Forget where the sweep stopped, so the next call starts at the oldest item.

    Exists for tests and for an operator who wants a deterministic pass right
    now (the rotation is an optimisation, never a correctness requirement).
    Without arguments it clears every (tenant, source) rotation.

    A sweep that is ALREADY running cannot put back the position this call just
    cleared: a sweep may only replace the position it read, and this is a write
    (see `_store_sweep_cursor`). What it can still do is store the end of the page
    it is serving if the rotation happened to be at its start already — which is
    where a fresh rotation would leave it anyway.
    """
    with _SWEEP_CURSOR_LOCK:
        if tenant_id is None and source is None:
            _SWEEP_CURSOR.clear()
        elif tenant_id is not None and source is not None:
            _SWEEP_CURSOR.pop((tenant_id, source), None)
        else:
            raise ValueError("pass both tenant_id and source, or neither")


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

    STARVATION, AND THE ROTATION THAT FIXES IT
    ------------------------------------------
    `ORDER BY last_transition_at ASC ... LIMIT 50` gives the right priority
    (oldest silence first) and, on its own, a permanent blind spot: the recovery
    action does not touch `last_transition_at` — deliberately, since re-reading a
    thread is not a state transition and forging one would corrupt both the
    ordering and every "how long has this been stuck" read in the console. So
    the ranking never changes, every cycle selects the SAME oldest 50, and item
    51 is never fetched from the platform API. Not late: never. Recovery is
    exactly the feature whose failure nobody notices.

    The fix is to page through the ordered list instead of re-reading its head: a
    keyset cursor holds the sort key of the last row handed out, the next call
    resumes after it, and reaching the tail (a short page) drops the cursor so
    the following call starts over at the oldest. Every pending item is therefore
    visited within ceil(total / limit) cycles, `limit` still caps the API calls
    per cycle, no schema changes, and nothing is written anywhere.

    The cursor is process-local and advisory on purpose. Persisting it would mean
    a new table (a second source of truth to keep consistent with work_items) for
    something whose worst case is already harmless: a restart, or a second
    replica with its own cursor, just re-visits items sooner than needed, and
    re-ingestion dedupes on `event_id` — see `recorded_work_item_id`. Under- and
    over-visiting are not symmetric failures here; only under-visiting loses a
    human's answer.
    """
    key = (tenant_id, source)
    with _SWEEP_CURSOR_LOCK:
        position = _SWEEP_CURSOR.get(key, _SCAN_START)
    cursor_transition_at, cursor_id = position

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, source_ref, status, last_transition_at FROM work_items "
            "WHERE tenant_id = %s AND source = %s AND status = ANY(%s) "
            "  AND (COALESCE(last_transition_at, 'infinity'::timestamptz), id) "
            "      > (%s::timestamptz, %s) "
            # COALESCE(..., 'infinity') in the ORDER BY is the same thing
            # `NULLS LAST` used to say, written so the keyset predicate above can
            # be a single row comparison over the identical expression.
            "ORDER BY COALESCE(last_transition_at, 'infinity'::timestamptz) ASC, id ASC "
            "LIMIT %s",
            (
                tenant_id,
                source,
                list(RECOVERABLE_STATUSES),
                cursor_transition_at,
                cursor_id,
                limit,
            ),
        )
        rows = cur.fetchall()

    if len(rows) < limit:
        # Tail of the rotation (including an empty page, which is also what a
        # cursor left pointing past deleted or answered items looks like): the
        # next cycle must start at the oldest item again, never dead-end.
        #
        # It goes through the same check as an advance, which is what stops a
        # slower overlapping call from undoing the wrap: popping the key here
        # unconditionally left the map empty, and that call's late advance then
        # found no position to compare against and re-seated its stale one.
        _store_sweep_cursor(key, expected=position, new_cursor=None)
    else:
        last_id, _, _, last_transition_at = rows[-1]
        _store_sweep_cursor(
            key,
            expected=position,
            new_cursor=(last_transition_at or _NULL_TRANSITION_KEY, last_id),
        )

    return [
        {
            "work_item_id": r[0],
            "source_ref": r[1] if isinstance(r[1], dict) else {},
            "status": r[2],
        }
        for r in rows
    ]
