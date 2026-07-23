"""WSB-E2-T3 estendida — sequenciamento das novas sessoes:
Planner (read-only, ANTES do gate) -> [gate] -> Coder -> Tester -> L1 -> L2 -> PR.
Usa os nomes ACTIVITY_RUN_PLANNER_TURN/TESTER_TURN/L2_REVIEW (dse_contracts) via
Activities FAKE (WS-C/WS-E constroem as reais em paralelo).

Inclui a prova de P3 (NAO-NEGOCIAVEL): a sessao L2 recebe SO plan + diff final,
NUNCA o historico/instrucoes do Coder.
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


def _order(log: list[str], name: str) -> int:
    return log.index(name)


@pytest.mark.asyncio
async def test_session_sequence_planner_gate_coder_tester_l1_l2_pr(time_skipping_env):
    work_item_id = new_work_item_id("seq")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(plan_risk_class="low")
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
        await _wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    log = state.calls_log
    # ordem relativa exigida: planner < provision < coder < tester < l1 < l2 < pr
    assert _order(log, "run_planner_turn") < _order(log, "provision_sandbox")
    assert _order(log, "provision_sandbox") < _order(log, "run_coder_turn")
    assert _order(log, "run_coder_turn") < _order(log, "run_tester_turn")
    assert _order(log, "run_tester_turn") < _order(log, "run_l1_pipeline")
    assert _order(log, "run_l1_pipeline") < _order(log, "run_l2_review")
    assert _order(log, "run_l2_review") < _order(log, "finalize_pr")
    assert state.planner_calls == 1 and state.tester_calls == 1 and state.l2_calls == 1

    actions = read_audit_actions(work_item_id)
    for a in ("planner_completed", "tester_turn_completed", "l1_completed", "l2_review_completed"):
        assert a in actions


@pytest.mark.asyncio
async def test_l2_review_receives_only_plan_and_diff_not_coder_history(time_skipping_env):
    """P3: o payload que a sessao L2 recebe contem plan + diff e NADA do
    historico/instrucoes do Coder."""
    work_item_id = new_work_item_id("p3")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(plan_risk_class="low")
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
        await _wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        await handle.result()

    payload = state.last_l2_payload
    assert payload is not None
    # presente: plan artifact + diff final
    assert "plan" in payload and payload["plan"]
    # Reconciliação da integração da Fase 2: o input estrito da Activity L2
    # (RunL2ReviewInput, WS-C) declara `diff` — o workflow agora envia `diff`
    # (o diff resumido do Coder), não `diff_summary`/`files_changed`. P3 segue
    # sendo a propriedade sob teste (nenhum campo de histórico do Coder).
    assert "diff" in payload
    # AUSENTE (P3): nada do historico/instrucoes do Coder
    for forbidden in ("instructions", "clarification_notes", "objections",
                      "model_override", "files_changed", "sandbox_id"):
        assert forbidden not in payload, f"L2 recebeu '{forbidden}' — viola P3"


@pytest.mark.asyncio
async def test_l2_objections_cycle_back_to_coder_then_pass(time_skipping_env):
    work_item_id = new_work_item_id("l2obj")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(plan_risk_class="low", l2_fail_times=1)  # objeta 1x, depois passa
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
        await _wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    assert state.l2_calls == 2       # objetou, depois aprovou
    assert state.coder_turn_calls == 2  # a objecao voltou ao Coder
    actions = read_audit_actions(work_item_id)
    assert "l2_objections_raised" in actions
    assert "l2_changes_requested" in actions


@pytest.mark.asyncio
async def test_l2_objections_exhaust_cap_and_escalate(time_skipping_env):
    work_item_id = new_work_item_id("l2cap")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(plan_risk_class="low", l2_fail_times=999)  # sempre objeta
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
            l2_retry_cap=1,
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
        result = await handle.result()

    assert result.status == WorkItemStatus.escalated.value
    assert state.finalize_calls == 0  # nunca finalizou PR com objecoes abertas (P6)
    actions = read_audit_actions(work_item_id)
    assert "escalated" in actions
