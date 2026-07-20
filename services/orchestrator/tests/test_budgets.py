"""WSB-E4-T1 — budgets na admissao e em fronteiras de fase.

Prova (Postgres + Temporal reais; fronteiras WS-C/WS-E fakeadas):
  - o teto de budget e checado na admissao e em CADA fronteira; exaurido ->
    Failed com mensagem clara (nunca corta mid-Activity — P6);
  - o consumo e AGREGADO dos custos reportados pelo gateway (WS-D) via o
    `cost_usd` de cada resultado de Activity de modelo;
  - todo evento de budget vira audit (P8);
  - operador pode elevar o teto (raise_budget) e o WorkItem retoma sem
    recomecar (aplicado na proxima fronteira, nunca no meio de uma Activity).
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

from conftest import insert_work_item, new_work_item_id, read_audit_actions
from fakes import FakeControlPlane, build_fake_activities


async def _wait_for_status(handle, expected, attempts: int = 400) -> str:
    status = None
    for _ in range(attempts):
        status = await handle.query(WorkItemLifecycleWorkflow.get_status)
        if status in expected:
            return status
        await asyncio.sleep(0.05)
    raise AssertionError(f"status nunca chegou em {expected}, ultimo={status!r}")


@pytest.mark.asyncio
async def test_budget_exhausted_fails_cleanly_at_boundary_no_truncation(time_skipping_env):
    work_item_id = new_work_item_id("budgetx")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    # teto minusculo; o custo do Coder ja o estoura -> falha na PROXIMA fronteira
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
    # o Coder rodou 1x (custo reportado), mas a fronteira SEGUINTE barrou: nada
    # de PR/output truncado (P6).
    assert state.coder_turn_calls == 1
    assert state.finalize_calls == 0
    actions = read_audit_actions(work_item_id)
    assert "budget_admitted" in actions
    assert "budget_consumed" in actions            # agregou o custo do gateway
    assert "budget_boundary_denied" in actions      # barrou numa fronteira
    assert "budget_exhausted" in actions


@pytest.mark.asyncio
async def test_operator_raise_budget_resumes_without_restart(time_skipping_env):
    work_item_id = new_work_item_id("budgetraise")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    hang = asyncio.Event()
    # sem o raise, estouraria (coder 0.05 > teto 0.01) na fronteira do Tester
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

        # espera o Coder estar em andamento (hang), eleva o teto AGORA (nunca
        # interrompe a Activity em andamento), depois libera.
        for _ in range(200):
            if state.coder_turn_calls >= 1:
                break
            await asyncio.sleep(0.05)
        assert state.coder_turn_calls == 1
        await handle.signal("raise_budget", 5.0)
        hang.set()

        await _wait_for_status(handle, {"pr_ready"})  # retomou, nao falhou
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    actions = read_audit_actions(work_item_id)
    assert "budget_exhausted" not in actions  # o raise evitou o estouro


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
        await _wait_for_status(handle, {"pr_ready"})
        state_snapshot = await handle.query(WorkItemLifecycleWorkflow.get_state)
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    # coder(0.02) + tester(0.03) + l2(0.01) = 0.06 agregado do gateway
    assert abs(state_snapshot["spent_usd"] - 0.06) < 1e-9
