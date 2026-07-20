"""WSB-E3-T2/T3 — gate de aprovacao de plano por risk class + rejection path.

Prova (contra Postgres + Temporal reais, so as fronteiras WS-C/WS-E fakeadas):
  - risco baixo -> auto-aprova por politica (sem gate, sem humano);
  - risco alto -> estaciona em `awaiting_plan_approval`, resolve aprovador pela
    cascata, e so segue apos SIGNAL_PLAN_APPROVAL (P1: nenhuma decisao por LLM);
  - cascata de aprovadores VAZIA -> Blocked (JAMAIS auto-aprova por ausencia);
  - classificacao deterministica de risco (defesa em profundidade): um plano
    que toca `migrations/` e high AINDA que o Planner diga low;
  - rejection path (3 rotas): re_plan / re_clarify / cancel, todas auditadas
    com identidade + justificativa, nenhuma dispara implementacao sem passar
    de novo pelo gate correspondente.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from temporalio.worker import Worker

from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator import policy
from dse_orchestrator.local_activities import LOCAL_ACTIVITIES
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import (
    insert_work_item,
    new_work_item_id,
    read_audit_actions,
    read_gate_row,
)
from fakes import FakeControlPlane, build_fake_activities


@pytest.fixture(autouse=True)
def _reset_codeowners():
    """Isola o reader de CODEOWNERS entre testes (global trocavel)."""
    policy.set_codeowners_reader(None)
    yield
    policy.set_codeowners_reader(None)


def _with_codeowners(owners: list[str]):
    policy.set_codeowners_reader(lambda tenant_id, repo: "* " + " ".join(owners))


async def _wait_for_status(handle, expected, attempts: int = 400) -> str:
    status = None
    for _ in range(attempts):
        status = await handle.query(WorkItemLifecycleWorkflow.get_status)
        if status in expected:
            return status
        await asyncio.sleep(0.05)
    raise AssertionError(f"status nunca chegou em {expected}, ultimo={status!r}")


@pytest.mark.asyncio
async def test_low_risk_auto_approves_without_gate(time_skipping_env):
    work_item_id = new_work_item_id("low")
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
        await _wait_for_status(handle, {"pr_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    actions = read_audit_actions(work_item_id)
    assert "planner_completed" in actions
    assert "plan_auto_approved" in actions
    assert "awaiting_plan_approval" not in actions  # nunca estacionou
    gate = read_gate_row(work_item_id)
    assert gate[0] == "approved" and gate[1] is True  # status=approved, auto_approved=True


@pytest.mark.asyncio
async def test_high_risk_parks_and_requires_named_approval(time_skipping_env):
    work_item_id = new_work_item_id("high")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    _with_codeowners(["@alice", "@bob"])
    state = FakeControlPlane(plan_risk_class="high")
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)

        # estaciona no gate: NAO provisiona sandbox nem chama Coder ate aprovar
        await _wait_for_status(handle, {"awaiting_plan_approval"})
        assert state.provision_calls == 0
        assert state.coder_turn_calls == 0
        gate = read_gate_row(work_item_id)
        assert gate[0] == "pending"
        assert set(gate[2]) == {"@alice", "@bob"}  # resolved_approvers (CODEOWNERS)

        # aprovacao por humano nomeado -> segue
        await handle.signal("plan_approval",
                            {"verdict": "approved", "actor": "usr_alice"})
        await _wait_for_status(handle, {"pr_ready"})
        assert state.coder_turn_calls == 1
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    actions = read_audit_actions(work_item_id)
    assert "awaiting_plan_approval" in actions
    assert "plan_approved" in actions
    gate = read_gate_row(work_item_id)
    assert gate[0] == "approved" and gate[1] is False and gate[3] == "usr_alice"


@pytest.mark.asyncio
async def test_empty_approver_cascade_blocks_never_auto_approves(time_skipping_env):
    work_item_id = new_work_item_id("noappr")
    insert_work_item(work_item_id, tenant_id=f"tenant-noappr-{uuid.uuid4().hex[:6]}")
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    # sem CODEOWNERS e (assumindo) sem access bundle p/ este tenant efemero
    policy.set_codeowners_reader(lambda t, r: None)
    state = FakeControlPlane(plan_risk_class="high")
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id,
            tenant_id=f"tenant-noappr-{uuid.uuid4().hex[:6]}", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
        result = await handle.result()

    assert result.status == WorkItemStatus.blocked.value
    assert state.coder_turn_calls == 0  # nunca implementou
    actions = read_audit_actions(work_item_id)
    assert "plan_gate_no_approver_blocked" in actions
    assert "escalated" in actions
    assert "plan_auto_approved" not in actions  # JAMAIS auto-aprova por ausencia
    gate = read_gate_row(work_item_id)
    assert gate[0] == "blocked"


@pytest.mark.asyncio
async def test_migrations_path_forces_high_even_when_planner_says_low(time_skipping_env):
    """Classificacao deterministica de defesa em profundidade (P1): o Planner
    declara low, mas o plano toca `migrations/` -> gate de alto risco mesmo
    assim (um modelo sub-classificando nao rebaixa o gate)."""
    work_item_id = new_work_item_id("mighigh")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    _with_codeowners(["@dba"])
    state = FakeControlPlane(plan_risk_class="low",
                             plan_expected_files=["migrations/0099_x.sql", "app.py"])
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
        await _wait_for_status(handle, {"awaiting_plan_approval"})
        gate = read_gate_row(work_item_id)
        assert gate[6] == "high"  # risk_class efetivo, apesar do Planner dizer low
        await handle.signal("plan_approval", {"verdict": "approved", "actor": "usr_dba"})
        await _wait_for_status(handle, {"pr_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {})
        result = await handle.result()
    assert result.status == WorkItemStatus.done.value


@pytest.mark.asyncio
async def test_rejection_cancel_is_terminal_failed_and_audited(time_skipping_env):
    work_item_id = new_work_item_id("rejcancel")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    _with_codeowners(["@alice"])
    state = FakeControlPlane(plan_risk_class="high")
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
        await _wait_for_status(handle, {"awaiting_plan_approval"})
        await handle.signal("plan_approval", {
            "verdict": "rejected", "route": "cancel", "actor": "usr_alice",
            "justification": "fora de escopo regulatorio",
        })
        result = await handle.result()

    assert result.status == WorkItemStatus.failed.value
    assert state.coder_turn_calls == 0  # rejeicao nunca dispara implementacao
    actions = read_audit_actions(work_item_id)
    assert "plan_rejected" in actions
    assert "plan_rejected_cancelled" in actions
    gate = read_gate_row(work_item_id)
    assert gate[0] == "rejected" and gate[4] == "cancel"
    assert gate[5] == "fora de escopo regulatorio"  # justificativa registrada


@pytest.mark.asyncio
async def test_rejection_re_plan_reruns_planner_then_gate(time_skipping_env):
    work_item_id = new_work_item_id("rejreplan")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    _with_codeowners(["@alice"])
    state = FakeControlPlane(plan_risk_class="high")
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
        await _wait_for_status(handle, {"awaiting_plan_approval"})
        assert state.planner_calls == 1
        # rejeita pedindo re_plan -> planner roda de novo e re-estaciona no gate
        await handle.signal("plan_approval", {
            "verdict": "rejected", "route": "re_plan", "actor": "usr_alice",
            "justification": "revise o blast radius",
        })
        # aguarda o RE-planejamento (o status ainda esta awaiting do 1o parking;
        # esperamos o efeito observavel de que o Planner rodou de novo).
        for _ in range(400):
            if state.planner_calls == 2:
                break
            await asyncio.sleep(0.05)
        assert state.planner_calls == 2  # RE-planejou
        await _wait_for_status(handle, {"awaiting_plan_approval"})
        assert state.coder_turn_calls == 0  # nunca implementou sem passar pelo gate
        # agora aprova
        await handle.signal("plan_approval", {"verdict": "approved", "actor": "usr_alice"})
        await _wait_for_status(handle, {"pr_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    actions = read_audit_actions(work_item_id)
    assert "plan_rejected_route_re_plan" in actions


@pytest.mark.asyncio
async def test_rejection_re_clarify_returns_to_clarification_gate(time_skipping_env):
    work_item_id = new_work_item_id("rejreclar")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    _with_codeowners(["@alice"])
    state = FakeControlPlane(plan_risk_class="high")
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
            clarification_reminder_hours=100.0, clarification_escalation_days=100.0,
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
        await _wait_for_status(handle, {"awaiting_plan_approval"})
        await handle.signal("plan_approval", {
            "verdict": "rejected", "route": "re_clarify", "actor": "usr_alice",
            "justification": "criterios de aceite ambiguos",
        })
        # volta ao gate de clarificacao (needs_clarification) — NAO a implementacao.
        # re_clarify reabre a rodada de clarificacao (limpa acceptance_criteria).
        await _wait_for_status(handle, {"needs_clarification"})
        assert state.coder_turn_calls == 0
        actions = read_audit_actions(work_item_id)
        assert "plan_rejected_route_re_clarify" in actions

        # responder a clarificacao re-abre planner + gate (nunca pula p/ impl):
        await handle.signal("clarification_answer", {"acceptance_criteria": "agora claro"})
        await _wait_for_status(handle, {"awaiting_plan_approval"})
        assert state.planner_calls == 2  # re-planejou apos re-clarificar
        assert state.coder_turn_calls == 0  # ainda nao implementou (passou pelo gate)
        # encerra limpo via cancel de operador (nao e o foco do teste)
        await handle.signal("cancel", "fim do teste")
        result = await handle.result()
    assert result.status in (WorkItemStatus.failed.value, WorkItemStatus.escalated.value)
