"""WSE-E6-T15 — workflow resume from a review comment (core of UC4).

Runs against the REAL Temporal on localhost:7233 (we never mock durability/
signaling — that is the whole point of the system). A minimal test workflow
stands in for WS-B's real `WorkItemLifecycleWorkflow` (which may not exist or be
stable yet during parallel development), just to prove that
`handle_review_event` delivers the right signal to the right workflow
(workflow_id == work_item_id).
"""
from __future__ import annotations

import uuid

import psycopg2
import pytest
from temporalio import workflow
from temporalio.client import Client
from temporalio.worker import Worker

from dse_contracts import Actor, ConversationEvent, EventKind, Platform

from dse_validation.review_signal import REVIEW_DECISION_SIGNAL_NAME, handle_review_event, interpret_review_decision

TEMPORAL_ADDRESS = "localhost:7233"


@workflow.defn
class ReviewSignalProbeWorkflow:
    """Stands in for WS-B's real WorkItemLifecycleWorkflow for this test only:
    it waits on exactly the same signal name (`review_decision`) that the
    production workflow will define (WSB-E3-T4) and returns the received payload."""

    def __init__(self) -> None:
        self._decision: dict | None = None

    @workflow.signal(name=REVIEW_DECISION_SIGNAL_NAME)
    async def review_decision(self, payload: dict) -> None:
        self._decision = payload

    @workflow.run
    async def run(self) -> dict:
        await workflow.wait_condition(lambda: self._decision is not None)
        return self._decision


def _count_rows(query: str, params: tuple) -> int:
    conn = psycopg2.connect("postgresql://dse_app:dse_app_dev_only@localhost:5432/dse")
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchone()[0]
    finally:
        conn.close()


def _make_event(kind: EventKind, source_ref: dict, content_snapshot: str) -> ConversationEvent:
    return ConversationEvent.build(
        platform=Platform.github,
        thread_key="acme/repo#99",
        message_id=f"c-{uuid.uuid4().hex[:8]}",
        kind=kind,
        source_ref=source_ref,
        actor=Actor(platform_user_id="gh:reviewer1", resolved_principal="usr_reviewer1"),
        content_snapshot=content_snapshot,
        signature_verified=True,
    )


class TestInterpretReviewDecisionIsDeterministic:
    def test_approval_event_kind_is_approved(self):
        event = _make_event(EventKind.approval, {"repo": "acme/repo", "pr_number": 1}, "LGTM, approving")
        assert interpret_review_decision(event) == "approved"

    def test_formal_changes_requested_review_is_changes_requested(self):
        event = _make_event(
            EventKind.review_comment,
            {"repo": "acme/repo", "pr_number": 1, "review_state": "changes_requested"},
            "please rename this variable",
        )
        assert interpret_review_decision(event) == "changes_requested"

    def test_plain_comment_without_formal_state_is_not_a_decision(self):
        event = _make_event(
            EventKind.review_comment, {"repo": "acme/repo", "pr_number": 1}, "nice work, just a nit"
        )
        assert interpret_review_decision(event) is None


@pytest.mark.asyncio
async def test_approval_signals_the_correct_workflow_by_work_item_id():
    client = await Client.connect(TEMPORAL_ADDRESS)
    task_queue = f"wse-test-{uuid.uuid4().hex[:8]}"
    work_item_id = f"wi_test_{uuid.uuid4().hex[:8]}"
    tenant_id = f"acme-test-{uuid.uuid4().hex[:8]}"

    async with Worker(client, task_queue=task_queue, workflows=[ReviewSignalProbeWorkflow]):
        handle = await client.start_workflow(
            ReviewSignalProbeWorkflow.run, id=work_item_id, task_queue=task_queue
        )

        event = _make_event(EventKind.approval, {"repo": "acme/repo", "pr_number": 1}, "LGTM")
        signaled = await handle_review_event(event, work_item_id, tenant_id, client)
        assert signaled is True

        result = await handle.result()
        # Found during Phase 1 integration: the real workflow (WS-B) reads
        # payload["verdict"], not payload["decision"] — fixed in
        # review_signal.py; this assertion follows the same fix.
        assert result["verdict"] == "approved"
        assert result["event_id"] == event.event_id


@pytest.mark.asyncio
async def test_changes_requested_signals_and_resumes_with_correct_decision():
    client = await Client.connect(TEMPORAL_ADDRESS)
    task_queue = f"wse-test-{uuid.uuid4().hex[:8]}"
    work_item_id = f"wi_test_{uuid.uuid4().hex[:8]}"
    tenant_id = f"acme-test-{uuid.uuid4().hex[:8]}"

    async with Worker(client, task_queue=task_queue, workflows=[ReviewSignalProbeWorkflow]):
        handle = await client.start_workflow(
            ReviewSignalProbeWorkflow.run, id=work_item_id, task_queue=task_queue
        )
        event = _make_event(
            EventKind.review_comment,
            {"repo": "acme/repo", "pr_number": 1, "review_state": "changes_requested"},
            "rename this please",
        )
        signaled = await handle_review_event(event, work_item_id, tenant_id, client)
        assert signaled is True

        result = await handle.result()
        assert result["verdict"] == "changes_requested"


@pytest.mark.asyncio
async def test_plain_review_comment_creates_no_work_item_and_no_pr_and_does_not_touch_temporal():
    """Core of the UC4 proof: a plain review comment is not a decision — it must
    not create a WorkItem, must not create a PR, and must not even attempt to
    signal a workflow (see `assert signaled is False` before any Temporal client
    call inside `handle_review_event`)."""
    client = await Client.connect(TEMPORAL_ADDRESS)
    work_item_id = f"wi_test_{uuid.uuid4().hex[:8]}"
    tenant_id = f"acme-test-{uuid.uuid4().hex[:8]}"

    before_work_items = _count_rows("SELECT count(*) FROM work_items WHERE tenant_id = %s", (tenant_id,))
    before_pr_tracking = _count_rows(
        "SELECT count(*) FROM wse_pr_tracking WHERE work_item_id = %s", (work_item_id,)
    )
    assert before_work_items == 0
    assert before_pr_tracking == 0

    # NO workflow is started for this work_item_id on purpose — if
    # handle_review_event tried to signal anyway, we would get a
    # "workflow not found" error from the real Temporal instead of a quiet False.
    event = _make_event(
        EventKind.review_comment, {"repo": "acme/repo", "pr_number": 2}, "looks fine overall, ship it eventually"
    )
    signaled = await handle_review_event(event, work_item_id, tenant_id, client)
    assert signaled is False

    after_work_items = _count_rows("SELECT count(*) FROM work_items WHERE tenant_id = %s", (tenant_id,))
    after_pr_tracking = _count_rows(
        "SELECT count(*) FROM wse_pr_tracking WHERE work_item_id = %s", (work_item_id,)
    )
    assert after_work_items == before_work_items == 0
    assert after_pr_tracking == before_pr_tracking == 0
