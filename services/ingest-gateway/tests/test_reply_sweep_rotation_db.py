"""The reply-sweep rotation against real Postgres.

test_reply_sweep_rotation.py proves the coverage property with a fake that
emulates the keyset page. What only Postgres can prove is that the page really is
one: the row comparison against `('-infinity'::timestamptz, '')`, and NULL
`last_transition_at` sorting last through `COALESCE(..., 'infinity')` instead of
`NULLS LAST`.

Needs the `dse_app` role (same as the rest of this suite); it errors on the
connection where Postgres is not available.
"""
from __future__ import annotations

import json
import uuid

import pytest

from ingest_gateway.reconcile import pending_reply_work_items, reset_reply_sweep_cursor

SOURCE = "slack"


@pytest.fixture(autouse=True)
def _fresh_rotation():
    reset_reply_sweep_cursor()
    yield
    reset_reply_sweep_cursor()


def _insert(
    conn,
    tenant_id: str,
    *,
    transition_seconds_ago: float | None,
    status: str = "needs_clarification",
) -> str:
    """`transition_seconds_ago=None` leaves `last_transition_at` NULL — an item
    admitted and never moved, which the ordering must place last."""
    work_item_id = f"wi_test_{uuid.uuid4().hex[:12]}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO work_items
                (id, tenant_id, source, source_ref, requester, status, idempotency_key,
                 last_transition_at)
            VALUES (%s, %s, %s, %s::jsonb, 'usr_test_requester', %s, %s,
                    CASE WHEN %s::float8 IS NULL THEN NULL
                         ELSE now() - make_interval(secs => %s::float8) END)
            """,
            (
                work_item_id,
                tenant_id,
                SOURCE,
                json.dumps({"channel": "C1", "thread_ts": f"{uuid.uuid4().int % 10**10}.000100"}),
                status,
                f"idem_{work_item_id}",
                transition_seconds_ago,
                transition_seconds_ago,
            ),
        )
    conn.commit()
    return work_item_id


def _sweep(conn, tenant_id: str, *, limit: int) -> list[str]:
    return [
        item["work_item_id"]
        for item in pending_reply_work_items(
            conn, tenant_id=tenant_id, source=SOURCE, limit=limit
        )
    ]


def test_consecutive_sweeps_cover_every_pending_item(db_conn, tenant_id):
    """The starvation regression: with limit=2 the third item was never read from
    the platform API, on any cycle, ever."""
    ids = [_insert(db_conn, tenant_id, transition_seconds_ago=3600 * (5 - i)) for i in range(5)]

    visited = _sweep(db_conn, tenant_id, limit=2)
    visited += _sweep(db_conn, tenant_id, limit=2)
    visited += _sweep(db_conn, tenant_id, limit=2)

    assert sorted(visited) == sorted(ids)


def test_the_first_page_is_still_the_longest_waiting(db_conn, tenant_id):
    oldest = _insert(db_conn, tenant_id, transition_seconds_ago=7200)
    _insert(db_conn, tenant_id, transition_seconds_ago=60)

    assert _sweep(db_conn, tenant_id, limit=1) == [oldest]


def test_items_never_transitioned_sort_last_and_are_still_reached(db_conn, tenant_id):
    with_transition = _insert(db_conn, tenant_id, transition_seconds_ago=7200)
    never_moved = _insert(db_conn, tenant_id, transition_seconds_ago=None)

    assert _sweep(db_conn, tenant_id, limit=1) == [with_transition]
    assert _sweep(db_conn, tenant_id, limit=1) == [never_moved]


def test_the_rotation_wraps_at_the_tail(db_conn, tenant_id):
    oldest = _insert(db_conn, tenant_id, transition_seconds_ago=7200)
    newest = _insert(db_conn, tenant_id, transition_seconds_ago=60)

    assert _sweep(db_conn, tenant_id, limit=1) == [oldest]
    assert _sweep(db_conn, tenant_id, limit=1) == [newest]
    assert _sweep(db_conn, tenant_id, limit=1) == []
    assert _sweep(db_conn, tenant_id, limit=1) == [oldest]


def test_a_plan_approval_is_never_in_the_page(db_conn, tenant_id):
    """Unchanged and non-negotiable: re-reading is exactly what the TOCTOU
    defense forbids for an approval."""
    _insert(db_conn, tenant_id, transition_seconds_ago=7200, status="awaiting_plan_approval")

    assert _sweep(db_conn, tenant_id, limit=50) == []
