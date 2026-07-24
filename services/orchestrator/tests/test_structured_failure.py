"""Fase 2 (plano 09) — classificação ESTRUTURADA de falha fail-closed.

Antes o workflow decidia por substring da mensagem (_FAIL_CLOSED_MARKERS);
agora decide pelo `type` do ApplicationError (dse.failure.* + legados). Estes
testes provam as três células:
  1. type canônico com mensagem NEUTRA (nenhuma palavra-marcador) → fail-closed
     limpo — a prova de que a mensagem não decide mais nada;
  2. type legado (EgressFailClosed/ProviderBillingError) → mesma classificação
     (replay/raisers antigos);
  3. type desconhecido e mensagem neutra → exaustão normal (NÃO fail-closed) —
     um erro comum nunca vira recusa de política por acidente.
"""
from __future__ import annotations

import uuid

import pytest
from temporalio.worker import Worker

from dse_contracts.failure import FailureClass, failure_type
from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator.local_activities import LOCAL_ACTIVITIES
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow, _failure_class_from

from conftest import insert_work_item, new_work_item_id, read_audit_actions
from fakes import FakeControlPlane, build_fake_activities


async def _run_to_result(env, state, work_item_id):
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)
    async with Worker(env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
        )
        handle = await env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
        return await handle.result()


@pytest.mark.asyncio
async def test_canonical_type_with_neutral_message_fails_closed(time_skipping_env):
    """A mensagem NÃO contém nenhum marcador de substring — só o type decide."""
    work_item_id = new_work_item_id("sfc")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        plan_risk_class="low",
        fail_closed_on={"run_coder_turn": {
            "times": 999,
            "marker": "model path refused by server-side gate",  # zero marcadores
            "type": failure_type(FailureClass.policy_fail_closed),
        }},
    )
    result = await _run_to_result(time_skipping_env, state, work_item_id)
    assert result.status == WorkItemStatus.failed.value
    assert "fail_closed" in (result.detail or "")
    assert state.finalize_calls == 0
    actions = read_audit_actions(work_item_id)
    assert "model_path_fail_closed_detected" in actions


@pytest.mark.asyncio
async def test_unknown_type_neutral_message_is_not_fail_closed(time_skipping_env):
    """Erro comum (type fora do vocabulário, mensagem neutra) segue o caminho
    de exaustão/escalação — nunca é promovido a recusa de política."""
    work_item_id = new_work_item_id("sfx")
    insert_work_item(work_item_id)
    state = FakeControlPlane(
        plan_risk_class="low",
        fail_closed_on={"run_coder_turn": {
            "times": 999,
            "marker": "unexpected null pointer in adapter",  # neutro
            "type": "SomeRandomBug",
        }},
    )
    result = await _run_to_result(time_skipping_env, state, work_item_id)
    assert result.status in (WorkItemStatus.failed.value, WorkItemStatus.escalated.value)
    assert "fail_closed" not in (result.detail or "")
    actions = read_audit_actions(work_item_id)
    assert "model_path_fail_closed_detected" not in actions


def test_failure_class_from_walks_cause_chain_and_maps_legacy():
    class _Err(Exception):
        def __init__(self, type_name=None, cause=None):
            self.type = type_name
            self.cause = cause

    canonical = _Err(cause=_Err(type_name=failure_type(FailureClass.budget_denied)))
    assert _failure_class_from(canonical) is FailureClass.budget_denied

    legacy = _Err(cause=_Err(type_name="ProviderBillingError"))
    assert _failure_class_from(legacy) is FailureClass.provider_billing
    assert _failure_class_from(_Err(type_name="EgressFailClosed")) is FailureClass.policy_fail_closed

    assert _failure_class_from(_Err(type_name="ValueError")) is None
    assert _failure_class_from(_Err()) is None
