"""Same guarantees as test_stranded.py, against real Postgres.

The fakes prove the decisions; this file proves the STATEMENTS — the lateral
`max(audit_log.ts)` and its `FILTER (WHERE action = ...)` sibling, the
`created_at` fallback for an item with no ledger row, the interval arithmetic,
the `state_version` bump, and the guards that make a second escalation a no-op
even after something un-escalates the item. Those are exactly the parts a fake
cannot vouch for.

Needs the `dse_app` role (same as the rest of this suite); it errors on the
connection where Postgres is not available.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from ingest_gateway.stranded import escalate_stranded, stranded_work_items

IDLE_40H = 40 * 3600
ACTOR = "system:ingest-gateway-stranded-sweep"


def _insert_work_item(
    conn, tenant_id: str, *, status: str, created_hours_ago: float = 48.0, source: str = "slack"
) -> str:
    work_item_id = f"wi_test_{uuid.uuid4().hex[:12]}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO work_items
                (id, tenant_id, source, source_ref, requester, status, idempotency_key, created_at)
            VALUES (%s, %s, %s, %s::jsonb, 'usr_test_requester', %s, %s,
                    now() - make_interval(secs => %s))
            """,
            (
                work_item_id,
                tenant_id,
                source,
                json.dumps({"channel": "C1", "thread_ts": f"{uuid.uuid4().int % 10**10}.000100"}),
                status,
                f"idem_{work_item_id}",
                float(created_hours_ago) * 3600.0,
            ),
        )
    conn.commit()
    return work_item_id


def _insert_audit_row(
    conn, tenant_id: str, work_item_id: str, *, hours_ago: float, action: str = "test_event"
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO audit_log (ts, work_item_id, tenant_id, actor, action, details)
            VALUES (now() - make_interval(secs => %s), %s, %s, 'system:test', %s,
                    '{}'::jsonb)
            """,
            (float(hours_ago) * 3600.0, work_item_id, tenant_id, action),
        )
    conn.commit()


def _set_status(conn, work_item_id: str, status: str) -> None:
    """Un-escalates an item the way any un-guarded writer of `work_items.status`
    would: no check on the current value, and no audit row. The column has no
    CHECK constraint and more than one writer, so this is reachable whatever
    discipline the orchestrator's own writer keeps."""
    with conn.cursor() as cur:
        cur.execute("UPDATE work_items SET status = %s WHERE id = %s", (status, work_item_id))
    conn.commit()


def _state_version(conn, work_item_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT state_version FROM work_items WHERE id = %s", (work_item_id,))
        return int(cur.fetchone()[0])


def _status(conn, work_item_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM work_items WHERE id = %s", (work_item_id,))
        return cur.fetchone()[0]


def _stranded_ids(conn, tenant_id: str, *, idle_for_seconds: int = IDLE_40H - 3600) -> list[str]:
    return [
        item["work_item_id"]
        for item in stranded_work_items(
            conn, tenant_id=tenant_id, idle_for_seconds=idle_for_seconds, limit=50
        )
    ]


def test_the_four_statuses_from_the_incident_are_detected(db_conn, tenant_id):
    ids = {}
    for status in ("implementing", "queued", "new", "validating"):
        wi_id = _insert_work_item(db_conn, tenant_id, status=status)
        _insert_audit_row(db_conn, tenant_id, wi_id, hours_ago=40)
        ids[status] = wi_id

    detected = _stranded_ids(db_conn, tenant_id)

    assert sorted(detected) == sorted(ids.values())


def test_a_recent_audit_event_means_the_item_is_alive(db_conn, tenant_id):
    """The one thing that distinguishes a live task from an abandoned one: a live
    task keeps writing to the ledger."""
    wi_id = _insert_work_item(db_conn, tenant_id, status="implementing")
    _insert_audit_row(db_conn, tenant_id, wi_id, hours_ago=40)
    _insert_audit_row(db_conn, tenant_id, wi_id, hours_ago=0.1)

    assert _stranded_ids(db_conn, tenant_id) == []


def test_an_item_with_no_audit_row_falls_back_to_created_at(db_conn, tenant_id):
    old = _insert_work_item(db_conn, tenant_id, status="new", created_hours_ago=48)
    fresh = _insert_work_item(db_conn, tenant_id, status="new", created_hours_ago=0.05)

    detected = _stranded_ids(db_conn, tenant_id)

    assert detected == [old]
    assert fresh not in detected


def test_terminal_and_human_wait_items_are_never_detected(db_conn, tenant_id):
    """`review_ready`/`merge_pending`/`pr_ready` are in this list because a LIVE
    workflow parks on an untimed `wait_condition` there and writes nothing while
    the reviewer takes their time. Escalating those would move every open PR under
    review into a terminal status."""
    for status in (
        "done", "failed", "blocked", "escalated",
        "needs_clarification", "awaiting_plan_approval", "awaiting_repo_selection",
        "review_ready", "merge_pending", "pr_ready",
    ):
        wi_id = _insert_work_item(db_conn, tenant_id, status=status)
        _insert_audit_row(db_conn, tenant_id, wi_id, hours_ago=40)

    assert _stranded_ids(db_conn, tenant_id) == []


def test_the_engine_owed_states_stay_detectable(db_conn, tenant_id):
    """The other side of that line: nobody is waiting on a human in `pr_open`,
    `ci_pending` or `review_feedback` — the engine owes the next step (evidence
    pipeline, a CI poll that audits every cycle, a fix turn), so silence there is
    the symptom this sweep exists for."""
    ids = set()
    for status in ("pr_open", "ci_pending", "review_feedback"):
        wi_id = _insert_work_item(db_conn, tenant_id, status=status)
        _insert_audit_row(db_conn, tenant_id, wi_id, hours_ago=40)
        ids.add(wi_id)

    assert set(_stranded_ids(db_conn, tenant_id)) == ids


def test_another_tenants_stranded_item_is_not_returned(db_conn, tenant_id):
    other_tenant = f"test_tenant_{uuid.uuid4().hex[:8]}"
    mine = _insert_work_item(db_conn, tenant_id, status="implementing")
    _insert_audit_row(db_conn, tenant_id, mine, hours_ago=40)
    theirs = _insert_work_item(db_conn, other_tenant, status="implementing")
    _insert_audit_row(db_conn, other_tenant, theirs, hours_ago=40)

    assert _stranded_ids(db_conn, tenant_id) == [mine]


def test_the_report_carries_the_last_event_and_the_idle_seconds(db_conn, tenant_id):
    wi_id = _insert_work_item(db_conn, tenant_id, status="implementing")
    _insert_audit_row(db_conn, tenant_id, wi_id, hours_ago=40)

    (item,) = stranded_work_items(
        db_conn, tenant_id=tenant_id, idle_for_seconds=IDLE_40H - 3600, limit=50
    )

    assert item["work_item_id"] == wi_id
    assert item["status"] == "implementing"
    assert item["source"] == "slack"
    assert item["source_ref"]["channel"] == "C1"
    expected_last_event = datetime.now(timezone.utc) - timedelta(hours=40)
    assert abs((item["last_event_at"] - expected_last_event).total_seconds()) < 300
    assert IDLE_40H - 300 < item["idle_seconds"] < IDLE_40H + 300


def test_the_oldest_silence_comes_first_and_the_limit_caps_the_page(db_conn, tenant_id):
    quieter = _insert_work_item(db_conn, tenant_id, status="implementing")
    _insert_audit_row(db_conn, tenant_id, quieter, hours_ago=60)
    louder = _insert_work_item(db_conn, tenant_id, status="queued")
    _insert_audit_row(db_conn, tenant_id, louder, hours_ago=40)

    page = stranded_work_items(
        db_conn, tenant_id=tenant_id, idle_for_seconds=IDLE_40H - 3600, limit=1
    )

    assert [item["work_item_id"] for item in page] == [quieter]


def test_detection_writes_no_audit_row(db_conn, tenant_id):
    """A timer must not narrate non-events into an append-only ledger."""
    wi_id = _insert_work_item(db_conn, tenant_id, status="implementing")
    _insert_audit_row(db_conn, tenant_id, wi_id, hours_ago=40)

    stranded_work_items(db_conn, tenant_id=tenant_id, idle_for_seconds=IDLE_40H - 3600, limit=50)
    db_conn.commit()

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM audit_log WHERE tenant_id = %s AND work_item_id = %s",
            (tenant_id, wi_id),
        )
        assert cur.fetchone()[0] == 1  # only the row the test wrote


def test_escalation_is_written_once_however_many_times_it_runs(db_conn, tenant_id):
    wi_id = _insert_work_item(db_conn, tenant_id, status="implementing")
    _insert_audit_row(db_conn, tenant_id, wi_id, hours_ago=40)
    kwargs = dict(work_item_id=wi_id, tenant_id=tenant_id, idle_seconds=IDLE_40H, actor=ACTOR)

    assert escalate_stranded(db_conn, **kwargs) is True
    assert escalate_stranded(db_conn, **kwargs) is False
    assert escalate_stranded(db_conn, **kwargs) is False

    assert _status(db_conn, wi_id) == "escalated"
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT details FROM audit_log "
            "WHERE tenant_id = %s AND work_item_id = %s AND action = 'work_item_escalated_stranded'",
            (tenant_id, wi_id),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0]["reason"] == "no_live_workflow"
    assert rows[0][0]["status_before"] == "implementing"


def test_an_escalated_item_stops_being_detected(db_conn, tenant_id):
    """Closes the loop that made the audit spam possible: once escalated, the
    item is terminal and the next sweep does not see it at all."""
    wi_id = _insert_work_item(db_conn, tenant_id, status="implementing")
    _insert_audit_row(db_conn, tenant_id, wi_id, hours_ago=40)

    escalate_stranded(
        db_conn, work_item_id=wi_id, tenant_id=tenant_id, idle_seconds=IDLE_40H, actor=ACTOR
    )

    assert wi_id not in _stranded_ids(db_conn, tenant_id)


def test_the_status_change_bumps_state_version(db_conn, tenant_id):
    """Matches `local_activities.update_work_item_status`, the canonical writer:
    it bumps `state_version` on exactly the writes that change the status, and
    this is the only other statement that changes one."""
    wi_id = _insert_work_item(db_conn, tenant_id, status="implementing")
    _insert_audit_row(db_conn, tenant_id, wi_id, hours_ago=40)
    before = _state_version(db_conn, wi_id)

    escalate_stranded(
        db_conn, work_item_id=wi_id, tenant_id=tenant_id, idle_seconds=IDLE_40H, actor=ACTOR
    )

    assert _state_version(db_conn, wi_id) == before + 1


def test_an_un_escalated_item_is_not_escalated_again(db_conn, tenant_id):
    """The status guard alone only holds while the item STAYS escalated. Something
    that puts it back into `implementing` with no audit row (see `_set_status`)
    would re-arm the timer, so suppression keys on the ledger instead — append-only
    per migrations/0028, and therefore not undoable by a status write."""
    wi_id = _insert_work_item(db_conn, tenant_id, status="implementing")
    _insert_audit_row(db_conn, tenant_id, wi_id, hours_ago=40)
    kwargs = dict(work_item_id=wi_id, tenant_id=tenant_id, idle_seconds=IDLE_40H, actor=ACTOR)

    assert escalate_stranded(db_conn, **kwargs) is True
    _set_status(db_conn, wi_id, "implementing")

    assert escalate_stranded(db_conn, **kwargs) is False
    assert _status(db_conn, wi_id) == "implementing"  # left where the other writer put it


def test_an_item_that_really_restarted_can_be_escalated_again(db_conn, tenant_id):
    """Suppression must not be permanent, or a task that genuinely came back to
    life and stranded a second time would never be reported. The guard is "our row
    is the newest", so any other audit row lifts it."""
    wi_id = _insert_work_item(db_conn, tenant_id, status="implementing")
    _insert_audit_row(db_conn, tenant_id, wi_id, hours_ago=40)
    kwargs = dict(work_item_id=wi_id, tenant_id=tenant_id, idle_seconds=IDLE_40H, actor=ACTOR)

    assert escalate_stranded(db_conn, **kwargs) is True
    _set_status(db_conn, wi_id, "implementing")
    # Real work, audited by somebody else, and then silence again.
    _insert_audit_row(db_conn, tenant_id, wi_id, hours_ago=0, action="sandbox_provisioned")

    assert escalate_stranded(db_conn, **kwargs) is True
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM audit_log WHERE tenant_id = %s AND work_item_id = %s "
            "AND action = 'work_item_escalated_stranded'",
            (tenant_id, wi_id),
        )
        assert cur.fetchone()[0] == 2


def test_detection_skips_an_item_whose_newest_ledger_row_is_our_escalation(db_conn, tenant_id):
    """Detection has to agree with the action, or every cycle would hand the
    caller an item that `escalate_stranded` then refuses — a sweep that looks busy
    and reports work that is already reported. Rows are backdated here because the
    interesting case is an item that has been silent for 40 hours WITH our row as
    the last word."""
    wi_id = _insert_work_item(db_conn, tenant_id, status="implementing")
    _insert_audit_row(db_conn, tenant_id, wi_id, hours_ago=41)
    _insert_audit_row(
        db_conn, tenant_id, wi_id, hours_ago=40, action="work_item_escalated_stranded"
    )

    assert wi_id not in _stranded_ids(db_conn, tenant_id)


def test_detection_reports_it_again_once_something_else_writes(db_conn, tenant_id):
    wi_id = _insert_work_item(db_conn, tenant_id, status="implementing")
    _insert_audit_row(
        db_conn, tenant_id, wi_id, hours_ago=41, action="work_item_escalated_stranded"
    )
    _insert_audit_row(db_conn, tenant_id, wi_id, hours_ago=40, action="sandbox_provisioned")

    assert wi_id in _stranded_ids(db_conn, tenant_id)


@pytest.mark.parametrize("status", ["done", "needs_clarification"])
def test_escalation_refuses_terminal_and_human_wait_items(status, db_conn, tenant_id):
    wi_id = _insert_work_item(db_conn, tenant_id, status=status)

    assert escalate_stranded(
        db_conn, work_item_id=wi_id, tenant_id=tenant_id, idle_seconds=IDLE_40H, actor=ACTOR
    ) is False
    assert _status(db_conn, wi_id) == status


def test_escalation_of_an_unknown_item_is_a_no_op(db_conn, tenant_id):
    assert escalate_stranded(
        db_conn, work_item_id="wi_test_does_not_exist", tenant_id=tenant_id,
        idle_seconds=IDLE_40H, actor=ACTOR,
    ) is False
