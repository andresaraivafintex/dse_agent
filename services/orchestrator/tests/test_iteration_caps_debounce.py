"""WSB-E4-T2 (Phase 3, ADR-26) — iteration caps + evidence refresh debounce.
Everything 100% deterministic (P1): counting, comparison and Temporal's durable
timers — no LLM decides a refresh/cap.

- Every `while` in the workflow has a tested cap: clarification
  (test_clarification_gate), L1 fix (test_lifecycle_happy_path), L2 objections
  and re_plan (test_phase2_sequence/test_plan_approval_gate) and — new here —
  REVIEW rounds (`review_round_cap`).
- Debounce (ADR-26): regenerate evidence ONLY when a new commit changes behavior
  (a fix cycle ran) OR on an explicit human request (`refresh_evidence` signal).
  6 review comments in one window produce at most 1 refresh — proven with the
  time-skipping environment (virtual window).
- Refresh cap (`evidence_refresh_cap`): exceeded -> CLEAN, audited decline (P6),
  without blocking the PR.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from temporalio.worker import Worker

from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator.local_activities import LOCAL_ACTIVITIES
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import insert_work_item, new_work_item_id, read_audit_actions, wait_for_status
from fakes import FakeControlPlane, build_fake_activities


async def _wait_until(predicate, attempts: int = 400, msg: str = "condition never became true"):
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(msg)


def _wf_input(work_item_id: str, **kw) -> WorkItemLifecycleInput:
    return WorkItemLifecycleInput(
        work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
        repo="acme/repo", base_branch="main", acceptance_criteria="crit", **kw,
    )


@pytest.mark.asyncio
async def test_six_review_comments_in_window_trigger_at_most_one_refresh(time_skipping_env):
    """ADR-26 (literal acceptance criterion from the statement): 6 review
    comments in one debounce window -> ONE fix cycle + ONE evidence refresh
    (never 6 rebuilds). Virtual 60s window fast-forwarded by time-skipping."""
    work_item_id = new_work_item_id("deb6")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(plan_risk_class="low",
                             coder_files_changed=["frontend/App.tsx"])
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run,
            _wf_input(work_item_id, evidence_debounce_seconds=60.0),
            id=work_item_id, task_queue=task_queue)

        # wait for the implementation phase (same execution as the review loop
        # — no continue_as_new between them, so signals are not lost) and send
        # the 6 comments BEFORE the review loop consumes them: they arrive as a
        # single pending batch.
        await wait_for_status(handle, {"implementing", "validating", "review_ready"})
        for i in range(6):
            await handle.signal("review_comment",
                                {"verdict": "changes_requested", "comment": f"tweak {i}"})

        # 1 batch -> 1 fix cycle -> 1 re-finalize of the SAME PR. The debounce
        # window is a DURABLE timer of 60 virtual seconds: we advance the
        # time-skipping server's clock in steps until the timer fires (auto-skip
        # only happens while the client is awaiting handle.result()).
        for _ in range(120):
            if state.finalize_calls >= 2:
                break
            await time_skipping_env.sleep(2)
        assert state.finalize_calls >= 2, "the batch fix cycle never re-finalized the PR"
        await wait_for_status(handle, {"review_ready"})

        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    # 6 comments -> a SINGLE Coder fix turn (1 initial + 1 fix)
    assert state.coder_turn_calls == 2
    # and AT MOST 1 evidence refresh (1 initial + 1 post-fix = 2 previews)
    assert state.trigger_preview_calls == 2
    assert state.demo_evidence_calls == 2
    assert state.finalize_calls == 2  # SAME PR re-finalized once

    actions = read_audit_actions(work_item_id)
    assert "review_comments_debounced" in actions
    # exactly 1 fix event and 1 refresh beyond the initial one
    assert actions.count("coder_fix_applied") == 1


@pytest.mark.asyncio
async def test_review_comments_alone_never_refresh_evidence(time_skipping_env):
    """ADR-26, the other side: `approved` (a comment that produces NO commit)
    does not regenerate evidence — a refresh only comes from a fix cycle or an
    explicit human request."""
    work_item_id = new_work_item_id("norefresh")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(plan_risk_class="low",
                             coder_files_changed=["frontend/App.tsx"])
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run,
            _wf_input(work_item_id, evidence_debounce_seconds=60.0),
            id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    assert state.trigger_preview_calls == 1  # ONLY the initial evidence


@pytest.mark.asyncio
async def test_human_refresh_evidence_signal_triggers_single_refresh(time_skipping_env):
    """ADR-26: the `refresh_evidence` signal (explicit human request) is the
    only refresh trigger without a new commit — it re-runs the pipeline ONCE,
    with no extra Coder turn."""
    work_item_id = new_work_item_id("humref")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(plan_risk_class="low",
                             coder_files_changed=["frontend/App.tsx"])
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _wf_input(work_item_id),
            id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"review_ready"})
        # wait for the INITIAL evidence to run (the pr_ready status is written
        # before the pipeline; a refresh sent before the pipeline starts would
        # legitimately be absorbed by it — debounce semantics)
        await _wait_until(lambda: state.trigger_preview_calls >= 1,
                          msg="the initial evidence run never happened")

        await handle.signal("refresh_evidence", {})
        await _wait_until(lambda: state.trigger_preview_calls >= 2,
                          msg="the human refresh never triggered the pipeline")

        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    assert state.trigger_preview_calls == 2
    assert state.coder_turn_calls == 1  # a human refresh does NOT run the Coder
    actions = read_audit_actions(work_item_id)
    assert "evidence_refresh_requested_by_human" in actions


@pytest.mark.asyncio
async def test_review_round_cap_escalates_never_loops_forever(time_skipping_env):
    """WSB-E4-T2: the review loop is capped by `review_round_cap` — once
    exhausted it escalates (never an infinite loop, by construction)."""
    work_item_id = new_work_item_id("revcap")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(plan_risk_class="low")
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run,
            _wf_input(work_item_id, review_round_cap=1, coder_retry_cap=99),
            id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"review_ready"})

        # round 1: within the cap — normal fix
        await handle.signal("review_comment", {"verdict": "changes_requested", "comment": "r1"})
        await _wait_until(lambda: state.finalize_calls >= 2,
                          msg="the first fix cycle never completed")
        await wait_for_status(handle, {"review_ready"})

        # round 2: exceeds the cap -> escalated
        await handle.signal("review_comment", {"verdict": "changes_requested", "comment": "r2"})
        result = await handle.result()

    assert result.status == WorkItemStatus.escalated.value
    assert "review_round_cap_exhausted" in (result.detail or "")
    assert state.coder_turn_calls == 2  # initial + round 1; round 2 never ran
    actions = read_audit_actions(work_item_id)
    assert "escalated" in actions


@pytest.mark.asyncio
async def test_evidence_refresh_cap_declines_cleanly_without_blocking(time_skipping_env):
    """ADR-26: refreshes beyond `evidence_refresh_cap` are DECLINED cleanly
    (audited, P6) — the evidence goes stale but the PR is never blocked."""
    work_item_id = new_work_item_id("refcap")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(plan_risk_class="low",
                             coder_files_changed=["frontend/App.tsx"])
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run,
            _wf_input(work_item_id, evidence_refresh_cap=1),
            id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"review_ready"})
        await _wait_until(lambda: state.trigger_preview_calls >= 1,
                          msg="the initial evidence run never happened")

        # refresh 1: within the cap
        await handle.signal("refresh_evidence", {})
        await _wait_until(lambda: state.trigger_preview_calls >= 2,
                          msg="refresh 1 never ran")

        # refresh 2: beyond the cap -> declined and audited, the pipeline does NOT run
        await handle.signal("refresh_evidence", {})
        await _wait_until(
            lambda: "evidence_refresh_declined_cap" in read_audit_actions(work_item_id),
            msg="the cap decline was never audited")

        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value  # never blocks (P6)
    assert state.trigger_preview_calls == 2  # initial + 1 refresh; the 2nd declined
