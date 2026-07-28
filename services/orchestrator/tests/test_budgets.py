"""WSB-E4-T1 — budgets at admission and at phase boundaries.

Proves (real Postgres + Temporal; WS-C/WS-E boundaries faked):
  - the budget ceiling is checked at admission and at EVERY boundary; exhausted
    -> Failed with a clear message (it never cuts mid-Activity — P6);
  - consumption is AGGREGATED from the costs the gateway (WS-D) reports via the
    `cost_usd` of each model Activity result;
  - every budget event becomes an audit row (P8);
  - the operator can raise the ceiling (raise_budget) and the WorkItem resumes
    without restarting (applied at the next boundary, never mid-Activity).
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from temporalio.worker import Worker

from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator.local_activities import LOCAL_ACTIVITIES, _default_work_item_max_usd
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import insert_work_item, new_work_item_id, read_audit_actions, wait_for_status
from fakes import FakeControlPlane, build_fake_activities


@pytest.mark.asyncio
async def test_budget_exhausted_fails_cleanly_at_boundary_no_truncation(time_skipping_env):
    work_item_id = new_work_item_id("budgetx")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    # tiny ceiling; the Coder's cost already blows it -> fails at the NEXT boundary
    state = FakeControlPlane(plan_risk_class="low", coder_cost_usd=0.05)
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
            budget_max_usd=0.01,
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
        result = await handle.result()

    assert result.status == WorkItemStatus.failed.value
    assert "budget_exhausted" in (result.detail or "")
    # the Coder ran once (cost reported), but the NEXT boundary stopped it: no
    # PR / truncated output (P6).
    assert state.coder_turn_calls == 1
    assert state.finalize_calls == 0
    actions = read_audit_actions(work_item_id)
    assert "budget_admitted" in actions
    assert "budget_consumed" in actions            # aggregated the gateway's cost
    assert "budget_boundary_denied" in actions      # stopped at a boundary
    assert "budget_exhausted" in actions


@pytest.mark.asyncio
async def test_operator_raise_budget_resumes_without_restart(time_skipping_env):
    work_item_id = new_work_item_id("budgetraise")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    hang = asyncio.Event()
    # without the raise it would blow the budget (coder 0.05 > ceiling 0.01) at
    # the Tester boundary
    state = FakeControlPlane(plan_risk_class="low", coder_cost_usd=0.05,
                             coder_turn_hang_event=hang)
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
            budget_max_usd=0.01,
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)

        # wait for the Coder to be in progress (hang), raise the ceiling NOW (it
        # never interrupts the running Activity), then release it.
        for _ in range(200):
            if state.coder_turn_calls >= 1:
                break
            await asyncio.sleep(0.05)
        assert state.coder_turn_calls == 1
        await handle.signal("raise_budget", 5.0)
        hang.set()

        await wait_for_status(handle, {"review_ready"})  # resumed, did not fail
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    actions = read_audit_actions(work_item_id)
    assert "budget_exhausted" not in actions  # the raise avoided the overrun


@pytest.mark.asyncio
async def test_budget_consumption_aggregates_gateway_costs(time_skipping_env):
    work_item_id = new_work_item_id("budgetagg")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(plan_risk_class="low",
                             coder_cost_usd=0.02, tester_cost_usd=0.03, l2_cost_usd=0.01)
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
            budget_max_usd=10.0,
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"review_ready"})
        state_snapshot = await handle.query(WorkItemLifecycleWorkflow.get_state)
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    # coder(0.02) + tester(0.03) + l2(0.01) = 0.06 aggregated from the gateway
    assert abs(state_snapshot["spent_usd"] - 0.06) < 1e-9


# ---------------------------------------------------------------------------
# Deployment default (the ceiling that applies when the work item carries none).
#
# Before this existed, `work_items.budget` was `{}` for every item, nothing ever
# wrote `max_usd`, and `_budget_boundary` returned early on `cap is None` — so in
# 20k audit rows on the pilot cluster there was not one `budget_boundary_ok`.
# The financial stop criterion had never been evaluated once.
# ---------------------------------------------------------------------------


def test_default_ceiling_applies_when_the_env_is_unset(monkeypatch):
    monkeypatch.delenv("DSE_DEFAULT_WORK_ITEM_MAX_USD", raising=False)
    assert _default_work_item_max_usd() == 25.0


def test_operator_can_disable_the_ceiling_with_zero(monkeypatch):
    """0 is the explicit opt-out: it restores the pre-default behaviour where
    nothing denominated in dollars can end a run."""
    monkeypatch.setenv("DSE_DEFAULT_WORK_ITEM_MAX_USD", "0")
    assert _default_work_item_max_usd() is None


def test_a_malformed_env_var_never_silently_removes_the_ceiling(monkeypatch):
    """A ConfigMap typo must not be a way to lose the cost guard."""
    monkeypatch.setenv("DSE_DEFAULT_WORK_ITEM_MAX_USD", "twenty-five")
    assert _default_work_item_max_usd() == 25.0
    monkeypatch.setenv("DSE_DEFAULT_WORK_ITEM_MAX_USD", "")
    assert _default_work_item_max_usd() == 25.0


def test_a_negative_ceiling_is_read_as_disabled_not_as_a_cap(monkeypatch):
    monkeypatch.setenv("DSE_DEFAULT_WORK_ITEM_MAX_USD", "-1")
    assert _default_work_item_max_usd() is None


@pytest.mark.asyncio
async def test_work_item_with_no_budget_is_stopped_by_the_deployment_default(
    time_skipping_env, monkeypatch
):
    """The regression that matters: an item whose JSONB budget is empty — i.e.
    every item in production — must now hit a ceiling instead of running until
    the review-round cap."""
    monkeypatch.setenv("DSE_DEFAULT_WORK_ITEM_MAX_USD", "0.01")
    work_item_id = new_work_item_id("budgetdef")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(plan_risk_class="low", coder_cost_usd=0.05)
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
            # deliberately NO budget_max_usd — this is the production shape
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
        result = await handle.result()

    assert result.status == WorkItemStatus.failed.value
    assert "budget_exhausted" in (result.detail or "")
    actions = read_audit_actions(work_item_id)
    assert "budget_default_applied" in actions   # the default was resolved and recorded
    assert "budget_boundary_denied" in actions
    assert state.finalize_calls == 0             # no PR from a run that overran


@pytest.mark.asyncio
async def test_an_explicit_work_item_budget_still_wins_over_the_default(
    time_skipping_env, monkeypatch
):
    """The default is a floor under items that have none — it must never
    override a ceiling the work item carries."""
    monkeypatch.setenv("DSE_DEFAULT_WORK_ITEM_MAX_USD", "0.01")
    work_item_id = new_work_item_id("budgetexp")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(plan_risk_class="low", coder_cost_usd=0.02,
                             tester_cost_usd=0.03, l2_cost_usd=0.01)
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
            budget_max_usd=10.0,
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    actions = read_audit_actions(work_item_id)
    assert "budget_default_applied" not in actions  # never consulted
