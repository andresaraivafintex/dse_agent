"""WSB-E2-T1/T3 — proves the happy-path state machine end to end: new ->
ready -> queued -> implementing -> validating -> pr_ready -> (CI green) ->
approved -> merged -> done. Uses FAKE Activities for the WS-C/WS-E boundaries
(still being built in parallel) and the REAL WS-B audit/status Activities
against the infra's real Postgres — it never mocks Postgres/Temporal.
"""
from __future__ import annotations

import uuid

import pytest
from temporalio.worker import Worker

from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator.local_activities import LOCAL_ACTIVITIES
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import insert_work_item, new_work_item_id, read_audit_actions, read_work_item, wait_for_status
from fakes import FakeControlPlane, build_fake_activities


@pytest.mark.asyncio
async def test_happy_path_reaches_done(time_skipping_env):
    work_item_id = new_work_item_id("happy")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"

    state = FakeControlPlane()
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(
        time_skipping_env.client,
        task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow],
        activities=activities,
    ):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id,
            tenant_id="test-tenant",
            requester="usr_test",
            repo="acme/repo",
            base_branch="main",
            acceptance_criteria="must do X and pass the tests",
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run,
            wf_input,
            id=work_item_id,
            task_queue=task_queue,
        )

        # the workflow must reach pr_ready and wait for human review — we do a
        # short poll via query until we see the expected status.
        await wait_for_status(handle, {"review_ready"})

        await handle.signal("review_comment", {"verdict": "approved"})
        # fine-grained states (spec §2): after approval the workflow parks on
        # merge_pending waiting for the human merge; `pr_ready` covers the
        # coarse fallback for runs without the fine-states patch.
        await wait_for_status(handle, {"merge_pending", "pr_ready"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})

        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    assert result.pr_number is not None

    row = read_work_item(work_item_id)
    assert row is not None
    assert row[0] == WorkItemStatus.done.value
    assert row[1] == result.pr_number

    actions = read_audit_actions(work_item_id)
    assert "intake_started" in actions
    assert "clarification_complete" in actions
    assert "pr_finalized" in actions
    assert "approved_awaiting_merge" in actions
    assert "merged_by_human" in actions

    assert state.provision_calls == 1
    assert state.finalize_calls == 1
    assert state.teardown_calls == 1  # teardown at the end (done)


@pytest.mark.asyncio
async def test_l1_failure_triggers_fix_loop_then_passes(time_skipping_env):
    work_item_id = new_work_item_id("l1retry")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"

    state = FakeControlPlane(l1_fail_times=2)  # fails twice, passes on the 3rd attempt
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(
        time_skipping_env.client,
        task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow],
        activities=activities,
    ):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id,
            tenant_id="test-tenant",
            requester="usr_test",
            repo="acme/repo",
            base_branch="main",
            acceptance_criteria="criterio presente",
            coder_retry_cap=3,
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run,
            wf_input,
            id=work_item_id,
            task_queue=task_queue,
        )
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    assert state.coder_turn_calls == 3  # 1 initial + 2 after L1 failures
    assert state.l1_calls == 3


@pytest.mark.asyncio
async def test_l1_failure_exhausts_retry_cap_and_fails(time_skipping_env):
    work_item_id = new_work_item_id("l1fail")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"

    # always fails — more than the retry cap
    state = FakeControlPlane(l1_fail_times=999)
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(
        time_skipping_env.client,
        task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow],
        activities=activities,
    ):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id,
            tenant_id="test-tenant",
            requester="usr_test",
            repo="acme/repo",
            base_branch="main",
            acceptance_criteria="criterio presente",
            coder_retry_cap=2,
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run,
            wf_input,
            id=work_item_id,
            task_queue=task_queue,
        )
        result = await handle.result()

    assert result.status == WorkItemStatus.failed.value
    assert "l1_failed_after" in (result.detail or "")
    row = read_work_item(work_item_id)
    assert row[0] == WorkItemStatus.failed.value
    actions = read_audit_actions(work_item_id)
    assert "coder_retry_cap_exhausted" in actions


