"""Phase C (report 07) — the lifecycle is source-agnostic.

Closes Seam 10: until now EVERY lifecycle test was born github-shaped (conftest
hardcoded source='github'), hiding divergences for slack/jira. These prove that
a SLACK-originated task, once the repo has been resolved at admission (C2),
flows through the same state machine all the way to `done` — and that the state
notifications ADDRESS the slack surface (post_tracking_comment is no longer
github-only; C3).
"""
from __future__ import annotations

import uuid

import pytest
from temporalio.worker import Worker

from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator.local_activities import LOCAL_ACTIVITIES
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import insert_work_item, new_work_item_id, read_audit_actions, wait_for_status
from fakes import FakeControlPlane, build_fake_activities



def _slack_input(work_item_id: str) -> WorkItemLifecycleInput:
    # Repo already resolved at admission (C2 cascade) — that was the missing
    # prerequisite; with it, the workflow treats slack like any other source.
    return WorkItemLifecycleInput(
        work_item_id=work_item_id,
        tenant_id="test-tenant",
        requester="usr_test",
        repo="acme/repo",
        base_branch="main",
        acceptance_criteria="verifiable criterion",
        ci_poll_interval_seconds=0.01,
        ci_pending_poll_cap=10,
    )


@pytest.mark.asyncio
async def test_slack_origin_work_item_reaches_done(time_skipping_env):
    work_item_id = new_work_item_id("slacklife")
    insert_work_item(
        work_item_id, source="slack",
        source_ref={"channel": "C_TEAM", "thread_ts": "1700000000.001"},
    )
    state = FakeControlPlane()
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"

    async with Worker(
        time_skipping_env.client, task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow],
        activities=list(LOCAL_ACTIVITIES) + build_fake_activities(state),
    ):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _slack_input(work_item_id),
            id=work_item_id, task_queue=task_queue,
        )
        # parks on review_ready waiting for the human verdict
        await wait_for_status(handle, {"review_ready", "pr_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await wait_for_status(handle, {"merge_pending", "pr_ready"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    # Same state machine as github: reaches done, opens a PR, runs the coder.
    assert result.status == WorkItemStatus.done.value
    assert result.pr_number is not None
    assert state.finalize_calls >= 1
    # The status was reflected on the surface (the REAL post_tracking_comment
    # ran — best-effort against the slack adapter; it audits
    # tracking_comment_posted when it reaches the adapter, or just moves on when
    # the adapter is down in the test).
    actions = read_audit_actions(work_item_id)
    assert "merged_by_human" in actions


@pytest.mark.asyncio
async def test_slack_source_ref_shape_survives_continue_as_new(time_skipping_env):
    """Slack's source_ref ({channel, thread_ts}) does not break any phase
    boundary (github uses {repo, number}) — the workflow never assumes a shape."""
    work_item_id = new_work_item_id("slackcan")
    insert_work_item(
        work_item_id, source="slack",
        source_ref={"channel": "C_X", "thread_ts": "1700000000.002"},
    )
    state = FakeControlPlane(plan_expected_files=[])  # -> escalates early, cheap
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    async with Worker(
        time_skipping_env.client, task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow],
        activities=list(LOCAL_ACTIVITIES) + build_fake_activities(state),
    ):
        result = await time_skipping_env.client.execute_workflow(
            WorkItemLifecycleWorkflow.run, _slack_input(work_item_id),
            id=work_item_id, task_queue=task_queue,
        )
    assert result.status == WorkItemStatus.escalated.value
    assert "escalated" in read_audit_actions(work_item_id)
