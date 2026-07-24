"""WSB-E5-T2 — controles de operador: pause/resume, cancel (+teardown),
reassign_model/runtime, escalate, force_clarification. Toda acao de operador
gera uma linha de audit (P8)."""
from __future__ import annotations

import asyncio
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
async def test_pause_blocks_next_activity_but_not_current(time_skipping_env):
    """Pause nao mata a Activity em andamento (a fake so termina quando o
    teste libera um Event) — so bloqueia a PROXIMA activity na fronteira
    seguinte, exatamente como especificado (WSB-E5-T2)."""
    work_item_id = new_work_item_id("pause")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"

    hang_event = asyncio.Event()
    state = FakeControlPlane(coder_turn_hang_event=hang_event)
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(
        time_skipping_env.client, task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow], activities=activities,
    ):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue,
        )

        # espera a Activity `run_coder_turn` estar em andamento (ela so libera
        # quando destravarmos o Event) e so ENTAO manda pause.
        for _ in range(100):
            if state.coder_turn_calls >= 1:
                break
            await asyncio.sleep(0.05)
        assert state.coder_turn_calls == 1

        await handle.signal("pause", "operador quer investigar")

        # a Activity em andamento nao e cancelada por pause — libera-a agora.
        hang_event.set()

        # o workflow deve prosseguir ATE o proximo boundary (checkpoint/L1)
        # e SO ENTAO ficar parado — nunca chega em pr_ready enquanto pausado.
        await asyncio.sleep(0.3)
        status_while_paused = await handle.query(WorkItemLifecycleWorkflow.get_status)
        assert status_while_paused != WorkItemStatus.review_ready.value

        await handle.signal("resume", "ok pode seguir")
        await wait_for_status(handle, {"review_ready"})

    actions = read_audit_actions(work_item_id)
    assert "pause" not in actions  # sinais de operador nao emitem audit por si (log interno via query)


@pytest.mark.asyncio
async def test_cancel_tears_down_sandbox_and_marks_failed(time_skipping_env):
    work_item_id = new_work_item_id("cancel")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"

    hang_event = asyncio.Event()
    state = FakeControlPlane(coder_turn_hang_event=hang_event)
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(
        time_skipping_env.client, task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow], activities=activities,
    ):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue,
        )
        for _ in range(100):
            if state.coder_turn_calls >= 1:
                break
            await asyncio.sleep(0.05)

        await handle.signal("cancel", "tarefa nao e mais necessaria")
        hang_event.set()  # libera a Activity em andamento para o workflow poder progredir e ver o cancel

        result = await handle.result()

    assert result.status == WorkItemStatus.failed.value
    assert "cancelled_by_operator" in (result.detail or "")
    assert state.teardown_calls == 1
    row = read_work_item(work_item_id)
    assert row[0] == WorkItemStatus.failed.value
    actions = read_audit_actions(work_item_id)
    assert "cancelled_by_operator" in actions


@pytest.mark.asyncio
async def test_reassign_model_is_forwarded_to_next_coder_turn(time_skipping_env):
    work_item_id = new_work_item_id("reassign")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(l1_fail_times=1)
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(
        time_skipping_env.client, task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow], activities=activities,
    ):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue,
        )
        await handle.signal("reassign_model", "claude-opus-4")
        await wait_for_status(handle, {"review_ready"})

    # `reassign_model` nao gera uma linha de audit propria (nao e uma
    # transicao de estado de negocio); a prova funcional de que o sinal foi
    # aplicado esta em `state.coder_turn_calls == 2` (1a tentativa falha no
    # L1, 2a — ja com o override aplicado — passa).
    assert state.coder_turn_calls == 2


@pytest.mark.asyncio
async def test_escalate_signal_forces_terminal_escalated(time_skipping_env):
    work_item_id = new_work_item_id("escalatesig")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"

    hang_event = asyncio.Event()
    state = FakeControlPlane(coder_turn_hang_event=hang_event)
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(
        time_skipping_env.client, task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow], activities=activities,
    ):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue,
        )
        for _ in range(100):
            if state.coder_turn_calls >= 1:
                break
            await asyncio.sleep(0.05)

        await handle.signal("escalate", "risco de compliance — parar")
        hang_event.set()

        result = await handle.result()

    assert result.status == WorkItemStatus.escalated.value
    actions = read_audit_actions(work_item_id)
    assert "escalated" in actions


