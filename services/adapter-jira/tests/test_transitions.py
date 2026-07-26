"""WSA-E5-T3 — per-ticket serialized transitions + enqueue dedup (against a
real Postgres; Jira via FakeJiraClient)."""
from __future__ import annotations

import psycopg2

from adapter_jira.backend import FakeJiraClient
from adapter_jira.transitions import TransitionWorker, enqueue_transition

DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
TENANT_ID = "test_tenant_jira_adapter"


def _fake_with_transitions(ticket: str) -> FakeJiraClient:
    return FakeJiraClient(
        transitions_by_ticket={
            ticket: [
                {"id": "11", "name": "Start", "to_status": "In Progress"},
                {"id": "21", "name": "Review", "to_status": "In Review"},
            ]
        },
        status_by_ticket={ticket: "To Do"},
    )


def _enqueue(ticket, target, dedup):
    conn = psycopg2.connect(DSN)
    try:
        ok = enqueue_transition(conn, tenant_id=TENANT_ID, ticket_key=ticket, target_status=target, dedup_key=dedup)
        conn.commit()
        return ok
    finally:
        conn.close()


def test_enqueue_is_idempotent_by_dedup_key():
    assert _enqueue("DSE-100", "In Progress", "dk-1") is True
    assert _enqueue("DSE-100", "In Progress", "dk-1") is False  # dedup

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM jira_transition_queue WHERE dedup_key='dk-1'")
        assert cur.fetchone()[0] == 1
    conn.close()


def test_transitions_applied_in_order_per_ticket():
    _enqueue("DSE-100", "In Progress", "dk-a")
    _enqueue("DSE-100", "In Review", "dk-b")

    client = _fake_with_transitions("DSE-100")
    processed = TransitionWorker(client).drain_once()
    assert processed == 2

    # applied in enqueue order (increasing id).
    assert [c["to_status"] for c in client.transition_calls] == ["In Progress", "In Review"]
    assert client.status_by_ticket["DSE-100"] == "In Review"

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM jira_transition_queue WHERE ticket_key='DSE-100' AND processed")
        assert cur.fetchone()[0] == 2
    conn.close()


def test_transition_to_a_status_the_project_workflow_lacks_is_settled_not_retried():
    """A missing column is a CONFIGURATION fact, not a transient failure.

    This used to be retried (attempts=1, row kept), which cost the real board
    five exhausted retries and, because transitions are ordered per ticket, froze
    everything queued behind them. The row is now settled with the reason
    recorded, and `jira_transition_unavailable` carries what the project's workflow
    does contain.

    Note `statuses_by_project`: the settle path requires the PROJECT to say the
    status does not exist. "No transition from where this card is standing" is not
    the same claim and is deliberately treated as transient — see
    tests/test_transition_unavailable.py for that contract, which runs without a
    database.
    """
    _enqueue("DSE-101", "Nonexistent Status", "dk-x")
    client = FakeJiraClient(
        transitions_by_ticket={"DSE-101": [{"id": "1", "name": "X", "to_status": "Done"}]},
        statuses_by_project={"DSE": ["To Do", "In Progress", "Done"]},
    )
    processed = TransitionWorker(client).drain_once()
    assert processed == 1  # settled (nothing was applied in Jira)
    assert client.transition_calls == []

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT processed, attempts, last_error FROM jira_transition_queue WHERE dedup_key='dk-x'")
        processed_flag, attempts, last_error = cur.fetchone()
        cur.execute(
            "SELECT count(*) FROM audit_log WHERE tenant_id=%s AND action='jira_transition_unavailable'",
            (TENANT_ID,),
        )
        audited = cur.fetchone()[0]
    conn.close()
    assert processed_flag is True  # will never be attempted again
    assert attempts == 0  # not a failed attempt
    assert "unavailable_target_status" in last_error  # visible, never silent (P8/P6)
    assert audited == 1
