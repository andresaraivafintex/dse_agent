"""WSB-E3-T1 — gate de clarificacao: reminder, escalacao por falta de
resposta, cap de rounds, e checklist deterministico (sem LLM). Usa o
ambiente de time-skipping do Temporal para acelerar os timers de
reminder/escalacao (configurados em HORAS/segundos curtos aqui, nao dias) e
provar o comportamento sem esperar tempo real de verdade.
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
async def test_clarification_completes_after_answer(time_skipping_env):
    work_item_id = new_work_item_id("clar-ok")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(FakeControlPlane())

    async with Worker(
        time_skipping_env.client, task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow], activities=activities,
    ):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo=None, base_branch=None, acceptance_criteria=None,
            clarification_reminder_hours=0.001,  # ~3.6s
            clarification_escalation_days=0.001,
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue,
        )
        await wait_for_status(handle, {"needs_clarification"})

        await handle.signal(
            "clarification_answer",
            {"repo": "acme/repo", "base_branch": "main", "acceptance_criteria": "deve fazer X"},
        )
        await wait_for_status(handle, {"review_ready"})

    actions = read_audit_actions(work_item_id)
    assert "clarification_requested" in actions
    assert "clarification_answer_received" in actions
    assert "clarification_complete" in actions
    row = read_work_item(work_item_id)
    assert row[0] in ("review_ready", "done")


@pytest.mark.asyncio
async def test_clarification_reminder_then_answer(time_skipping_env):
    """Timer de reminder dispara (sem resposta ainda), so DEPOIS a resposta
    chega e o workflow segue — prova que o reminder nao e terminal."""
    work_item_id = new_work_item_id("clar-remind")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(FakeControlPlane())

    async with Worker(
        time_skipping_env.client, task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow], activities=activities,
    ):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            acceptance_criteria=None,
            clarification_reminder_hours=1.0,  # curto o suficiente p/ time-skipping avancar rapido
            clarification_escalation_days=1.0,  # reminder == escalation --> so 1 janela
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue,
        )
        await wait_for_status(handle, {"needs_clarification"})

        # Nao respondemos ainda — avancamos o relogio do servidor de teste
        # explicitamente (time-skipping) alem do timer de reminder (1h) sem
        # esperar tempo real de verdade.
        from datetime import timedelta

        await time_skipping_env.sleep(timedelta(hours=1, minutes=5))
        actions = await _wait_for_audit_action(work_item_id, "clarification_reminder_sent")
        assert "clarification_reminder_sent" in actions

        await handle.signal(
            "clarification_answer",
            {"repo": "acme/repo", "base_branch": "main", "acceptance_criteria": "ok agora"},
        )
        await wait_for_status(handle, {"review_ready"})

    row = read_work_item(work_item_id)
    assert row[0] in ("review_ready", "done")


@pytest.mark.asyncio
async def test_clarification_escalates_without_any_response(time_skipping_env):
    work_item_id = new_work_item_id("clar-esc")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(FakeControlPlane())

    async with Worker(
        time_skipping_env.client, task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow], activities=activities,
    ):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            acceptance_criteria=None,
            clarification_reminder_hours=1.0,
            clarification_escalation_days=1.0 / 24.0,  # = 1h, igual ao reminder -> escala logo apos o reminder
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue,
        )
        result = await handle.result()

    assert result.status == WorkItemStatus.escalated.value
    actions = read_audit_actions(work_item_id)
    assert "clarification_reminder_sent" in actions
    assert "escalated" in actions
    row = read_work_item(work_item_id)
    assert row[0] == WorkItemStatus.escalated.value


@pytest.mark.asyncio
async def test_clarification_round_cap_escalates(time_skipping_env):
    """Responde sempre com o MESMO campo faltando (ex.: nunca manda
    acceptance_criteria) — o checklist nunca fecha, entao o cap de rounds
    estoura e escala (nunca segue adivinhando)."""
    work_item_id = new_work_item_id("clar-cap")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(FakeControlPlane())

    async with Worker(
        time_skipping_env.client, task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow], activities=activities,
    ):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo=None, base_branch=None, acceptance_criteria=None,
            clarification_round_cap=2,
            clarification_reminder_hours=1.0,
            clarification_escalation_days=10.0,  # nao deixa o timer de escalacao disparar primeiro
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue,
        )
        for _ in range(3):
            await wait_for_status(handle, {"needs_clarification"})
            # responde so o repo — base_branch/acceptance_criteria continuam faltando
            await handle.signal("clarification_answer", {"repo": "acme/repo"})

        result = await handle.result()

    assert result.status == WorkItemStatus.escalated.value
    assert "clarification_round_cap_exhausted" in (result.detail or "")
    actions = read_audit_actions(work_item_id)
    assert actions.count("clarification_answer_received") == 2  # cap=2


async def _wait_for_audit_action(work_item_id: str, action: str, attempts: int = 300) -> list[str]:
    import asyncio

    actions: list[str] = []
    for _ in range(attempts):
        actions = read_audit_actions(work_item_id)
        if action in actions:
            return actions
        await asyncio.sleep(0.05)
    raise AssertionError(f"acao de audit '{action}' nunca apareceu; vistas={actions}")
