"""P0 regressions from the first remediation sprint.

Exercises real Temporal and Postgres; only other services' boundaries use fakes,
and each fake decodes the payload through the canonical Pydantic model.
"""
from __future__ import annotations

import asyncio
import uuid

import psycopg2
import pytest
from temporalio.worker import Worker

from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator.local_activities import LOCAL_ACTIVITIES
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import DSN, insert_work_item, new_work_item_id, read_audit_actions, wait_for_status
from fakes import FakeControlPlane, build_fake_activities


def _input(work_item_id: str, **overrides) -> WorkItemLifecycleInput:
    values = dict(
        work_item_id=work_item_id,
        tenant_id="test-tenant",
        requester="usr_test",
        repo="acme/repo",
        base_branch="main",
        acceptance_criteria="criterio verificavel",
        ci_poll_interval_seconds=0.01,
        ci_pending_poll_cap=10,
        activity_retry_cap=2,
    )
    values.update(overrides)
    return WorkItemLifecycleInput(**values)


@pytest.mark.asyncio
async def test_empty_expected_files_never_reaches_coder_or_pr(time_skipping_env):
    work_item_id = new_work_item_id("emptyplan")
    insert_work_item(work_item_id)
    state = FakeControlPlane(plan_expected_files=[])
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"

    async with Worker(
        time_skipping_env.client,
        task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow],
        activities=list(LOCAL_ACTIVITIES) + build_fake_activities(state),
    ):
        result = await time_skipping_env.client.execute_workflow(
            WorkItemLifecycleWorkflow.run,
            _input(work_item_id),
            id=work_item_id,
            task_queue=task_queue,
        )

    assert result.status == WorkItemStatus.escalated.value
    assert state.coder_turn_calls == 0
    assert state.finalize_calls == 0
    assert "planner_contract_rejected" in read_audit_actions(work_item_id)


@pytest.mark.asyncio
async def test_tester_tests_ran_false_is_terminal_before_l1(time_skipping_env):
    work_item_id = new_work_item_id("notests")
    insert_work_item(work_item_id)
    state = FakeControlPlane(tester_tests_ran=False, tester_tests_passed=False)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"

    async with Worker(
        time_skipping_env.client,
        task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow],
        activities=list(LOCAL_ACTIVITIES) + build_fake_activities(state),
    ):
        result = await time_skipping_env.client.execute_workflow(
            WorkItemLifecycleWorkflow.run,
            _input(work_item_id),
            id=work_item_id,
            task_queue=task_queue,
        )

    assert result.status == WorkItemStatus.failed.value
    assert state.l1_calls == 0
    assert state.finalize_calls == 0
    assert "tester_tests_not_run" in read_audit_actions(work_item_id)


@pytest.mark.asyncio
async def test_ci_pending_polls_until_green_before_review(time_skipping_env):
    work_item_id = new_work_item_id("cipending")
    insert_work_item(work_item_id)
    state = FakeControlPlane(ci_sequence=["pending", "pending", "green"])
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"

    async with Worker(
        time_skipping_env.client,
        task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow],
        activities=list(LOCAL_ACTIVITIES) + build_fake_activities(state),
    ):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run,
            _input(work_item_id),
            id=work_item_id,
            task_queue=task_queue,
        )
        await wait_for_status(handle, {WorkItemStatus.review_ready.value})
        assert state.calls_log.count("consume_ci_status") == 3
        await handle.signal("review_comment", {"verdict": "approved"})
        await wait_for_status(handle, {WorkItemStatus.merge_pending.value})
        await handle.signal(
            "merged_by_human", {"merged_by": "usr_test", "pr_number": 1000}
        )
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    actions = read_audit_actions(work_item_id)
    assert actions.count("ci_pending") == 3
    assert actions.index("awaiting_human_review") > actions.index("ci_pending")


@pytest.mark.asyncio
async def test_unverified_merge_signal_is_rejected_and_wait_continues(time_skipping_env):
    work_item_id = new_work_item_id("badmerge")
    insert_work_item(work_item_id)
    state = FakeControlPlane()
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"

    async with Worker(
        time_skipping_env.client,
        task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow],
        activities=list(LOCAL_ACTIVITIES) + build_fake_activities(state),
    ):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run,
            _input(work_item_id),
            id=work_item_id,
            task_queue=task_queue,
        )
        await wait_for_status(handle, {WorkItemStatus.review_ready.value})
        await handle.signal("review_comment", {"verdict": "approved"})
        await wait_for_status(handle, {WorkItemStatus.merge_pending.value})

        await handle.signal("merged_by_human", {})
        for _ in range(100):
            if "merge_signal_rejected" in read_audit_actions(work_item_id):
                break
            await asyncio.sleep(0.025)
        assert await handle.query(WorkItemLifecycleWorkflow.get_status) == "merge_pending"
        assert "merge_signal_rejected" in read_audit_actions(work_item_id)

        await handle.signal(
            "merged_by_human", {"merged_by": "usr_test", "pr_number": 1000}
        )
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    assert "merge_signal_verified" in read_audit_actions(work_item_id)


@pytest.mark.asyncio
async def test_forged_merge_signal_refuted_by_github_api(time_skipping_env):
    """Plano 08 §F (F1): a signal that PASSES the envelope (correct pr_number)
    but whose PR is NOT merged according to the GitHub API is rejected (forged
    webhook) and the workflow keeps waiting; once the API confirms the merge, it
    completes."""
    work_item_id = new_work_item_id("forgedmerge")
    insert_work_item(work_item_id)
    state = FakeControlPlane()
    state.merge_verify_mode = "not_merged"  # the API refutes it (PR open, not merged)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"

    async with Worker(
        time_skipping_env.client,
        task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow],
        activities=list(LOCAL_ACTIVITIES) + build_fake_activities(state),
    ):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _input(work_item_id),
            id=work_item_id, task_queue=task_queue,
        )
        await wait_for_status(handle, {WorkItemStatus.review_ready.value})
        await handle.signal("review_comment", {"verdict": "approved"})
        await wait_for_status(handle, {WorkItemStatus.merge_pending.value})

        # VALID envelope (correct pr_number), but the API says "not merged" -> refuted
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        for _ in range(100):
            if "merge_signal_rejected" in read_audit_actions(work_item_id):
                break
            await asyncio.sleep(0.025)
        assert "merge_signal_rejected" in read_audit_actions(work_item_id)
        assert await handle.query(WorkItemLifecycleWorkflow.get_status) == "merge_pending"

        # now the API confirms the merge -> it completes
        state.merge_verify_mode = "verified"
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    assert "verify_merge_state" in state.calls_log
    assert "merge_signal_verified" in read_audit_actions(work_item_id)


@pytest.mark.asyncio
async def test_plan_risk_and_sha_boundaries_are_persisted_and_propagated(time_skipping_env):
    work_item_id = new_work_item_id("truth")
    insert_work_item(work_item_id)
    state = FakeControlPlane()
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"

    async with Worker(
        time_skipping_env.client,
        task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow],
        activities=list(LOCAL_ACTIVITIES) + build_fake_activities(state),
    ):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run,
            _input(work_item_id),
            id=work_item_id,
            task_queue=task_queue,
        )
        await wait_for_status(handle, {WorkItemStatus.review_ready.value})
        assert state.last_l1_payload is not None
        assert state.last_l1_payload["base_sha"] == "base0001"
        assert state.last_l1_payload["head_sha"].startswith("head")
        assert state.last_ci_payload["ref"] == state.last_ci_payload["head_sha"]

        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal(
            "merged_by_human", {"merged_by": "usr_test", "pr_number": 1000}
        )
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT plan, plan_hash, expected_files, risk_class, base_sha, head_sha, "
            "       ci_status, state_version FROM work_items WHERE id = %s",
            (work_item_id,),
        )
        plan, plan_hash, expected_files, risk, base_sha, head_sha, ci, version = cur.fetchone()
    assert plan and plan_hash and expected_files == ["app.py"]
    assert risk == "low"
    assert base_sha == "base0001" and head_sha.startswith("head")
    assert ci == "green" and version > 0
