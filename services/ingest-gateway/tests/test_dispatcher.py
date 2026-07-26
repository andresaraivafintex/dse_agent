"""WSA-E1-T3 — outbox dispatcher: `SELECT ... FOR UPDATE SKIP LOCKED` + real
Temporal `start_workflow` (no mocks — the real Postgres and Temporal from the
infra, per CONVENTIONS.md: never mock durability/idempotency).

Central test (core of the Phase 1 exit chaos test, NFR-01, intake side): 2
concurrent dispatchers (2 threads, each with its own event loop + Temporal
Client) draining the SAME ingest_events queue without duplicating or losing
anything.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg2
import pytest
from temporalio.client import Client

from ingest_gateway.dispatcher import Dispatcher

DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
TEMPORAL_ADDRESS = "localhost:7233"


def _insert_task_request_row(tenant_id: str, n: int) -> str:
    """Creates a work_item + ingest_event (kind=task_request) directly,
    simulating what `admit_work_item` would already have done — the dispatcher
    does not care who wrote the row."""
    work_item_id = f"wi_disp_{uuid.uuid4().hex[:12]}"
    event_id = f"evt_{uuid.uuid4().hex}"
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO work_items (id, tenant_id, source, source_ref, requester, idempotency_key)
            VALUES (%s, %s, 'slack', %s::jsonb, 'usr_test', %s)
            """,
            (work_item_id, tenant_id, json.dumps({"thread_ts": f"t{n}"}), f"idem_{work_item_id}"),
        )
        cur.execute(
            """
            INSERT INTO ingest_events (work_item_id, event_id, kind, payload)
            VALUES (%s, %s, 'task_request', %s::jsonb)
            """,
            (work_item_id, event_id, json.dumps({"n": n})),
        )
    conn.commit()
    conn.close()
    return work_item_id


def _run_dispatcher_drain_all(batch_size: int) -> int:
    async def _run():
        client = await Client.connect(TEMPORAL_ADDRESS)
        dispatcher = Dispatcher(client, batch_size=batch_size)
        return await dispatcher.drain_all()

    return asyncio.new_event_loop().run_until_complete(_run())


@pytest.fixture
def temporal_client():
    return asyncio.new_event_loop().run_until_complete(Client.connect(TEMPORAL_ADDRESS))


def test_single_dispatcher_starts_workflow_and_marks_processed(tenant_id, temporal_client):
    work_item_id = _insert_task_request_row(tenant_id, 1)

    dispatcher = Dispatcher(temporal_client, batch_size=10)
    processed = asyncio.new_event_loop().run_until_complete(dispatcher.drain_once())
    assert processed == 1

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT processed FROM ingest_events WHERE work_item_id = %s", (work_item_id,))
        assert cur.fetchone() == (True,)
        cur.execute(
            "SELECT 1 FROM audit_log WHERE work_item_id=%s AND action='dispatch_started'",
            (work_item_id,),
        )
        assert cur.fetchone() is not None
    conn.close()

    desc = asyncio.new_event_loop().run_until_complete(
        temporal_client.get_workflow_handle(work_item_id).describe()
    )
    assert desc.id == work_item_id


def test_duplicate_ingest_event_for_same_work_item_is_idempotent(tenant_id, temporal_client):
    """Simulates 2 outbox rows pointing to the SAME work_item (this should
    never happen via admit_work_item, which dedupes by event_id — but the
    dispatcher must be proof against it): the 2nd start_workflow attempt hits
    WorkflowAlreadyStartedError and is treated as an idempotent success, never
    re-raised."""
    work_item_id = f"wi_disp_{uuid.uuid4().hex[:12]}"
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO work_items (id, tenant_id, source, source_ref, requester, idempotency_key) "
            "VALUES (%s,%s,'slack','{}'::jsonb,'usr_test',%s)",
            (work_item_id, tenant_id, f"idem_{work_item_id}"),
        )
        for i in range(2):
            cur.execute(
                "INSERT INTO ingest_events (work_item_id, event_id, kind, payload) VALUES (%s,%s,'task_request','{}'::jsonb)",
                (work_item_id, f"evt_{work_item_id}_{i}", ),
            )
    conn.commit()
    conn.close()

    dispatcher = Dispatcher(temporal_client, batch_size=10)
    loop = asyncio.new_event_loop()
    processed = loop.run_until_complete(dispatcher.drain_all())
    assert processed == 2  # both rows marked processed, no crash

    check_conn = psycopg2.connect(DSN)
    with check_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM ingest_events WHERE work_item_id=%s AND processed", (work_item_id,))
        assert cur.fetchone()[0] == 2
        cur.execute(
            "SELECT action, count(*) FROM audit_log WHERE work_item_id=%s GROUP BY action", (work_item_id,)
        )
        actions = dict(cur.fetchall())
    check_conn.close()

    assert actions.get("dispatch_started") == 1
    assert actions.get("dispatch_deduped_already_started") == 1


def test_two_concurrent_dispatchers_drain_without_duplication_or_loss(tenant_id):
    """CORE of the NFR-01 chaos test (intake side): 20 distinct ingest_events,
    2 concurrent dispatchers (separate threads/processes, each with its own
    Temporal Client and Postgres connection) draining the SAME queue via
    `SELECT ... FOR UPDATE SKIP LOCKED`. At the end: all 20 rows
    processed=true, exactly once each (none duplicated, none lost), and exactly
    one Temporal workflow started per work_item (no WorkflowAlreadyStartedError
    should even occur here, since each work_item is unique — proving that SKIP
    LOCKED avoids the race before it even reaches Temporal)."""
    N = 20
    work_item_ids = [_insert_task_request_row(tenant_id, i) for i in range(N)]

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_run_dispatcher_drain_all, 3) for _ in range(2)]
        results = [f.result(timeout=60) for f in futures]

    # The test's 2 dispatchers together process AT MOST N. Strict equality
    # (== N) only holds when they are the ONLY consumers of the queue; in this
    # phase's shared environment the `dse_ingest_dispatcher` container
    # (run_forever) is also up and may drain part of the rows via the SAME
    # `SELECT ... FOR UPDATE SKIP LOCKED`. That does NOT violate NFR-01 — the
    # real invariant (no loss, no duplication, exactly-once) is proven by the
    # database assertions below, which hold for ANY number of concurrent
    # consumers (the test's 2 + the container). SKIP LOCKED guarantees one
    # consumer per row; which one processed it is irrelevant.
    assert sum(results) <= N
    assert sum(results) >= 0

    # Short wait: if the background container picked up (via SKIP LOCKED) the
    # rows the test's 2 dispatchers skipped, they may be in flight at the
    # instant the test dispatchers give up (3 empty rounds). Polling with a
    # timeout absorbs that window without masking real loss — if any are still
    # missing when the timeout expires, it is genuine loss and the assert fails.
    import time as _time

    deadline = _time.time() + 15
    conn = psycopg2.connect(DSN)
    while True:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM ingest_events WHERE work_item_id = ANY(%s) AND processed",
                (work_item_ids,),
            )
            done = cur.fetchone()[0]
        conn.commit()  # ends the snapshot so the next read sees recent commits
        if done == N or _time.time() >= deadline:
            break
        _time.sleep(0.25)

    with conn.cursor() as cur:
        assert done == N  # none lost — ALL processed (by any consumer)

        cur.execute(
            "SELECT count(*) FROM ingest_events WHERE work_item_id = ANY(%s)",
            (work_item_ids,),
        )
        assert cur.fetchone()[0] == N  # no duplicate row was created

        # Exactly one dispatch_started per work_item — no double race.
        cur.execute(
            """
            SELECT work_item_id, count(*) FROM audit_log
            WHERE work_item_id = ANY(%s) AND action = 'dispatch_started'
            GROUP BY work_item_id
            """,
            (work_item_ids,),
        )
        counts = {row[0]: row[1] for row in cur.fetchall()}
    conn.close()

    assert len(counts) == N
    assert all(c == 1 for c in counts.values())
