"""The reply sweep must eventually look at EVERY item waiting for a human.

The bug this pins: `pending_reply_work_items` ordered by `last_transition_at ASC
LIMIT 50` and nothing in the recovery path changes `last_transition_at` (correct
— re-reading a thread is not a state transition). So the ranking never moved,
every cycle re-read the same oldest 50 threads, and item 51 was never fetched
from the platform API. Not late: never. A human's answer sat there forever while
the reconciler looked busy.

The fix under test is a keyset rotation: each call resumes after the last row of
the previous one and wraps at the tail. The properties that matter are coverage
(the union of consecutive cycles is every pending item) and the blast-radius cap
(no cycle ever returns more than `limit`).

Postgres is faked, and the fake implements only the ordering/keyset semantics the
query relies on — the SQL fragments it emulates are asserted below so the fake
cannot silently drift away from the statement it stands for.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from ingest_gateway.reconcile import (
    RECOVERABLE_STATUSES,
    pending_reply_work_items,
    reset_reply_sweep_cursor,
)

T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _cleanup_test_rows():
    """Overrides the conftest fixture of the same name: nothing in this module
    touches Postgres, and that fixture opens a real connection at teardown."""
    yield


@pytest.fixture(autouse=True)
def _fresh_rotation():
    """The rotation cursor is module state, so a leftover position from another
    test would decide what the next one sees."""
    reset_reply_sweep_cursor()
    yield
    reset_reply_sweep_cursor()


def _sort_value(transition_at):
    """The `COALESCE(last_transition_at, 'infinity')` of the real query: NULL (and
    the 'infinity' sentinel the cursor stores for it) sorts after every real
    timestamp; '-infinity' starts a rotation before all of them."""
    if transition_at is None or transition_at == "infinity":
        return math.inf
    if transition_at == "-infinity":
        return -math.inf
    return transition_at.timestamp()


class _FakeCursor:
    """Emulates the pending-reply query: filter by tenant/source/status, keep only
    rows strictly after the keyset, order by (transition_at, id), cap by limit."""

    def __init__(self, table: list[tuple], seen_sql: list[str], conn: "_FakeConn") -> None:
        self._table = table
        self._seen_sql = seen_sql
        self._conn = conn
        self._rows: list[tuple] = []

    def execute(self, sql, params):
        self._seen_sql.append(sql)
        # Stands in for the round-trip: whatever this runs happens while THIS call
        # is still in flight, which is how an overlapping sweep is reproduced
        # deterministically instead of with threads.
        hook, self._conn.on_execute = self._conn.on_execute, None
        if hook is not None:
            hook()
        tenant_id, source, statuses, cursor_at, cursor_id, limit = params
        after = (_sort_value(cursor_at), cursor_id)
        matching = [
            row
            for row in self._table
            if row[4] == tenant_id and row[5] == source and row[2] in statuses
            and (_sort_value(row[3]), row[0]) > after
        ]
        matching.sort(key=lambda row: (_sort_value(row[3]), row[0]))
        # The query selects id, source_ref, status, last_transition_at.
        self._rows = [row[:4] for row in matching[:limit]]

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, table: list[tuple]) -> None:
        self._table = table
        self.seen_sql: list[str] = []
        # One-shot callback fired inside the next `execute`. See _FakeCursor.
        self.on_execute = None

    def cursor(self):
        return _FakeCursor(self._table, self.seen_sql, self)


def _row(
    work_item_id: str,
    *,
    minutes_ago: int | None = 0,
    status: str = "needs_clarification",
    tenant_id: str = "t1",
    source: str = "slack",
) -> tuple:
    """(id, source_ref, status, last_transition_at, tenant_id, source) — the last
    two are filter columns the real query never returns."""
    transition_at = None if minutes_ago is None else T0 - timedelta(minutes=minutes_ago)
    return (
        work_item_id,
        {"channel": "C1", "thread_ts": f"{work_item_id}.000"},
        status,
        transition_at,
        tenant_id,
        source,
    )


def _sweep(conn, *, limit: int, tenant_id: str = "t1", source: str = "slack") -> list[str]:
    items = pending_reply_work_items(conn, tenant_id=tenant_id, source=source, limit=limit)
    return [item["work_item_id"] for item in items]


def test_first_cycle_still_starts_at_the_oldest_items():
    """Priority is unchanged: the longest-waiting human is served first."""
    conn = _FakeConn([_row("wi_c", minutes_ago=10), _row("wi_a", minutes_ago=90),
                      _row("wi_b", minutes_ago=50)])

    assert _sweep(conn, limit=2) == ["wi_a", "wi_b"]


def test_second_cycle_reads_the_item_the_old_query_starved():
    """The regression itself: with limit=2 and three pending items, `wi_c` was
    never fetched. It must be the next cycle's work."""
    conn = _FakeConn([_row("wi_a", minutes_ago=90), _row("wi_b", minutes_ago=50),
                      _row("wi_c", minutes_ago=10)])

    assert _sweep(conn, limit=2) == ["wi_a", "wi_b"]
    assert _sweep(conn, limit=2) == ["wi_c"]


def test_rotation_wraps_back_to_the_oldest_after_the_tail():
    conn = _FakeConn([_row("wi_a", minutes_ago=90), _row("wi_b", minutes_ago=50),
                      _row("wi_c", minutes_ago=10)])

    _sweep(conn, limit=2)
    _sweep(conn, limit=2)

    assert _sweep(conn, limit=2) == ["wi_a", "wi_b"]


def test_every_pending_item_is_visited_within_one_rotation():
    """Coverage bound: ceil(total / limit) cycles see everything, once each."""
    table = [_row(f"wi_{i:02d}", minutes_ago=100 - i) for i in range(7)]
    conn = _FakeConn(table)

    visited: list[str] = []
    for _ in range(math.ceil(len(table) / 2)):
        visited.extend(_sweep(conn, limit=2))

    assert sorted(visited) == sorted(r[0] for r in table)
    assert len(visited) == len(set(visited))


def test_no_cycle_exceeds_the_limit():
    """The blast-radius guard is the reason `limit` exists — the rotation must not
    trade it away for coverage by widening the page."""
    conn = _FakeConn([_row(f"wi_{i:02d}", minutes_ago=100 - i) for i in range(9)])

    for _ in range(6):
        assert len(_sweep(conn, limit=3)) <= 3


def test_items_with_no_transition_timestamp_are_reached_and_not_repeated():
    """A NULL `last_transition_at` sorts last (an item that never transitioned).
    Two of them exist so the cursor has to advance THROUGH a NULL row: storing
    NULL as "start from the beginning" would re-serve the same one forever and
    starve the other."""
    conn = _FakeConn([_row("wi_old", minutes_ago=90), _row("wi_null_a", minutes_ago=None),
                      _row("wi_null_b", minutes_ago=None)])

    assert _sweep(conn, limit=1) == ["wi_old"]
    assert _sweep(conn, limit=1) == ["wi_null_a"]
    assert _sweep(conn, limit=1) == ["wi_null_b"]


def test_an_empty_page_resets_the_rotation():
    """Everything in the current window got answered while the sweep was mid
    rotation. The cursor now points past the end; the next cycle must start over
    rather than dead-end on empty pages forever."""
    table = [_row("wi_a", minutes_ago=90), _row("wi_b", minutes_ago=50)]
    conn = _FakeConn(table)

    assert _sweep(conn, limit=1) == ["wi_a"]
    table.remove(_row("wi_b", minutes_ago=50))
    assert _sweep(conn, limit=1) == []
    assert _sweep(conn, limit=1) == ["wi_a"]


def test_rotations_are_independent_per_tenant_and_source():
    """One adapter's sweep must not consume another's position — otherwise a busy
    Slack tenant would skip GitHub's oldest item."""
    conn = _FakeConn(
        [
            _row("wi_slack_a", minutes_ago=90, source="slack"),
            _row("wi_slack_b", minutes_ago=80, source="slack"),
            _row("wi_gh_a", minutes_ago=70, source="github"),
            _row("wi_other_tenant", minutes_ago=95, tenant_id="t2"),
        ]
    )

    assert _sweep(conn, limit=1) == ["wi_slack_a"]
    assert _sweep(conn, limit=1, source="github") == ["wi_gh_a"]
    assert _sweep(conn, limit=1, tenant_id="t2") == ["wi_other_tenant"]
    assert _sweep(conn, limit=1) == ["wi_slack_b"]


def test_a_slow_overlapping_sweep_cannot_rewind_the_rotation():
    """The sweep is an HTTP handler: two timers can overlap, and the cursor lock is
    NOT held across the query (holding it over a database round-trip would let one
    slow connection block every other tenant). So a call that started first can
    finish last, holding a position two pages behind — and a plain assignment would
    put the rotation back there, making the next cycle re-serve a page it had
    already passed. The advance is therefore a compare-and-set.

    Here the "slow" outer call is still inside its query while two other cycles run
    to completion; afterwards the rotation must be where the newer ones left it.
    """
    conn = _FakeConn([_row("wi_a", minutes_ago=90), _row("wi_b", minutes_ago=50),
                      _row("wi_c", minutes_ago=10)])

    def _overlapping_cycles():
        assert _sweep(conn, limit=1) == ["wi_a"]
        assert _sweep(conn, limit=1) == ["wi_b"]

    conn.on_execute = _overlapping_cycles
    assert _sweep(conn, limit=1) == ["wi_a"]  # the slow call, reading the old position

    assert _sweep(conn, limit=1) == ["wi_c"]


def test_a_late_advance_cannot_undo_the_tail_wrap():
    """The hole the first version of the compare-and-set left open: the wrap popped
    the cursor key OUTSIDE the compare, so a slow call finishing afterwards found
    no position to compare against and re-seated its own stale one — the rotation
    silently jumped back to the middle and re-served a page it had already passed
    instead of starting over at the oldest item.

    Here the slow call is one page behind while two cycles run its window to the
    tail. Its advance must be dropped, and the next cycle must be the oldest item.
    """
    conn = _FakeConn([_row("wi_a", minutes_ago=90), _row("wi_b", minutes_ago=50)])

    assert _sweep(conn, limit=1) == ["wi_a"]

    def _reach_the_tail():
        assert _sweep(conn, limit=1) == ["wi_b"]
        assert _sweep(conn, limit=1) == []  # wrap: the rotation is back at the start

    conn.on_execute = _reach_the_tail
    assert _sweep(conn, limit=1) == ["wi_b"]  # the slow call, one position behind

    assert _sweep(conn, limit=1) == ["wi_a"]


def test_the_tail_reset_is_still_allowed_to_move_the_position_backwards():
    """The compare-and-set must not swallow the wrap: reaching the tail is exactly
    the case where the position has to go back to the beginning, and blocking it
    would dead-end the rotation on empty pages forever."""
    conn = _FakeConn([_row("wi_a", minutes_ago=90), _row("wi_b", minutes_ago=50)])

    assert _sweep(conn, limit=1) == ["wi_a"]
    assert _sweep(conn, limit=1) == ["wi_b"]
    assert _sweep(conn, limit=1) == []  # tail: cursor dropped

    assert _sweep(conn, limit=1) == ["wi_a"]


def test_reset_starts_the_rotation_over():
    conn = _FakeConn([_row("wi_a", minutes_ago=90), _row("wi_b", minutes_ago=50)])

    assert _sweep(conn, limit=1) == ["wi_a"]
    reset_reply_sweep_cursor(tenant_id="t1", source="slack")

    assert _sweep(conn, limit=1) == ["wi_a"]


def test_reset_refuses_a_half_specified_key():
    """A cursor is keyed by (tenant, source); clearing "all of tenant t1" is not a
    thing this map can express, and silently clearing nothing would be worse."""
    with pytest.raises(ValueError):
        reset_reply_sweep_cursor(tenant_id="t1")


def test_only_reply_blocked_statuses_are_returned():
    """Unchanged contract, re-pinned because the query was rewritten: a plan
    approval is never recovered (a recovered approval is a decision manufactured
    from text nobody signed) and an item that is progressing is not the sweep's
    business."""
    conn = _FakeConn(
        [
            _row("wi_clarify", minutes_ago=90, status="needs_clarification"),
            _row("wi_repo", minutes_ago=80, status="awaiting_repo_selection"),
            _row("wi_approval", minutes_ago=70, status="awaiting_plan_approval"),
            _row("wi_working", minutes_ago=60, status="implementing"),
        ]
    )

    assert _sweep(conn, limit=10) == ["wi_clarify", "wi_repo"]
    assert set(RECOVERABLE_STATUSES) == {"needs_clarification", "awaiting_repo_selection"}


def test_row_shape_is_what_the_adapters_read():
    conn = _FakeConn([_row("wi_a", minutes_ago=90)])

    items = pending_reply_work_items(conn, tenant_id="t1", source="slack", limit=10)

    assert items == [
        {
            "work_item_id": "wi_a",
            "source_ref": {"channel": "C1", "thread_ts": "wi_a.000"},
            "status": "needs_clarification",
        }
    ]


def test_the_statement_is_a_keyset_scan_over_the_ordering_expression():
    """Keeps the fake honest: it emulates a keyset page over
    COALESCE(last_transition_at, 'infinity'), and if the real SQL stops being
    that, this suite is proving nothing."""
    conn = _FakeConn([_row("wi_a", minutes_ago=90)])

    _sweep(conn, limit=1)

    sql = conn.seen_sql[0]
    assert "COALESCE(last_transition_at, 'infinity'::timestamptz), id) " in sql
    assert "> (%s::timestamptz, %s)" in sql
    assert "ORDER BY COALESCE(last_transition_at, 'infinity'::timestamptz) ASC, id ASC" in sql
