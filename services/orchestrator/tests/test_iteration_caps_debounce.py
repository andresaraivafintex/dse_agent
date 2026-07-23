"""WSB-E4-T2 (Fase 3, ADR-26) — iteration caps + debounce de refresh de
evidencia. Tudo 100% deterministico (P1): contagem, comparacao e timers
duraveis do Temporal — nenhum LLM decide refresh/cap.

- Todo `while` do workflow tem cap testado: clarificacao
  (test_clarification_gate), fix L1 (test_lifecycle_happy_path), objecoes L2 e
  re_plan (test_phase2_sequence/test_plan_approval_gate) e — novo aqui —
  rounds de REVIEW (`review_round_cap`).
- Debounce (ADR-26): re-gerar evidencia SO quando um commit novo muda
  comportamento (fix cycle executado) OU a pedido humano explicito (signal
  `refresh_evidence`). 6 comentarios de review numa janela geram no maximo
  1 refresh — provado com o ambiente de time-skipping (janela virtual).
- Cap de refreshes (`evidence_refresh_cap`): excedido -> declina LIMPO e
  auditado (P6), sem bloquear o PR.
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
    from temporalio.service import RPCError

    status = None
    for _ in range(attempts):
        try:
            status = await handle.query(WorkItemLifecycleWorkflow.get_status)
        except RPCError:
            # query pode dar timeout transiente se aterrissar na janela de
            # continue_as_new no servidor de time-skipping — re-polla
            await asyncio.sleep(0.05)
            continue
        if status in expected:
            return status
        await asyncio.sleep(0.05)
    raise AssertionError(f"status nunca chegou em {expected}, ultimo={status!r}")


async def _wait_until(predicate, attempts: int = 400, msg: str = "condicao nunca ficou true"):
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
    """ADR-26 (aceitacao literal do enunciado): 6 comentarios de review numa
    janela de debounce -> UM ciclo de fix + UM refresh de evidencia (nunca 6
    rebuilds). Janela virtual de 60s acelerada pelo time-skipping."""
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

        # espera a fase de implementacao (mesma execucao que o review loop —
        # sem continue_as_new entre elas, sinais nao se perdem) e manda os 6
        # comentarios ANTES de o loop de review consumi-los: chegam como um
        # unico lote pendente.
        await _wait_for_status(handle, {"implementing", "validating", "review_ready"})
        for i in range(6):
            await handle.signal("review_comment",
                                {"verdict": "changes_requested", "comment": f"ajuste {i}"})

        # 1 lote -> 1 fix cycle -> 1 re-finalize do MESMO PR. A janela de
        # debounce e um timer DURAVEL de 60s virtuais: avancamos o relogio do
        # servidor de time-skipping em passos ate o timer disparar (o
        # auto-skip so acontece quando o cliente espera handle.result()).
        for _ in range(120):
            if state.finalize_calls >= 2:
                break
            await time_skipping_env.sleep(2)
        assert state.finalize_calls >= 2, "fix cycle do lote nunca re-finalizou o PR"
        await _wait_for_status(handle, {"review_ready"})

        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    # 6 comentarios -> UM unico turno de fix do Coder (1 inicial + 1 fix)
    assert state.coder_turn_calls == 2
    # e NO MAXIMO 1 refresh de evidencia (1 inicial + 1 pos-fix = 2 previews)
    assert state.trigger_preview_calls == 2
    assert state.demo_evidence_calls == 2
    assert state.finalize_calls == 2  # MESMO PR re-finalizado uma vez

    actions = read_audit_actions(work_item_id)
    assert "review_comments_debounced" in actions
    # exatamente 1 evento de fix e 1 refresh alem do initial
    assert actions.count("coder_fix_applied") == 1


@pytest.mark.asyncio
async def test_review_comments_alone_never_refresh_evidence(time_skipping_env):
    """ADR-26, o outro lado: `approved` (comentario que NAO gera commit) nao
    re-gera evidencia — refresh so por fix cycle ou pedido humano explicito."""
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
        await _wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    assert state.trigger_preview_calls == 1  # SO a evidencia inicial


@pytest.mark.asyncio
async def test_human_refresh_evidence_signal_triggers_single_refresh(time_skipping_env):
    """ADR-26: signal `refresh_evidence` (pedido humano explicito) e o unico
    gatilho de refresh sem commit novo — re-roda o pipeline UMA vez, sem
    nenhum turno extra do Coder."""
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
        await _wait_for_status(handle, {"review_ready"})
        # espera a evidencia INICIAL rodar (o status pr_ready e gravado antes
        # do pipeline; um refresh enviado antes do pipeline iniciar seria
        # legitimamente absorvido por ele — semantica do debounce)
        await _wait_until(lambda: state.trigger_preview_calls >= 1,
                          msg="evidencia inicial nunca rodou")

        await handle.signal("refresh_evidence", {})
        await _wait_until(lambda: state.trigger_preview_calls >= 2,
                          msg="refresh humano nunca disparou o pipeline")

        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    assert state.trigger_preview_calls == 2
    assert state.coder_turn_calls == 1  # refresh humano NAO roda Coder
    actions = read_audit_actions(work_item_id)
    assert "evidence_refresh_requested_by_human" in actions


@pytest.mark.asyncio
async def test_review_round_cap_escalates_never_loops_forever(time_skipping_env):
    """WSB-E4-T2: o loop de review e capado por `review_round_cap` — esgotado,
    escala (nunca um loop infinito por construcao)."""
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
        await _wait_for_status(handle, {"review_ready"})

        # round 1: dentro do cap — fix normal
        await handle.signal("review_comment", {"verdict": "changes_requested", "comment": "r1"})
        await _wait_until(lambda: state.finalize_calls >= 2,
                          msg="primeiro fix cycle nunca completou")
        await _wait_for_status(handle, {"review_ready"})

        # round 2: estoura o cap -> escalated
        await handle.signal("review_comment", {"verdict": "changes_requested", "comment": "r2"})
        result = await handle.result()

    assert result.status == WorkItemStatus.escalated.value
    assert "review_round_cap_exhausted" in (result.detail or "")
    assert state.coder_turn_calls == 2  # inicial + round 1; round 2 nunca rodou
    actions = read_audit_actions(work_item_id)
    assert "escalated" in actions


@pytest.mark.asyncio
async def test_evidence_refresh_cap_declines_cleanly_without_blocking(time_skipping_env):
    """ADR-26: refreshes alem de `evidence_refresh_cap` sao DECLINADOS limpo
    (auditado, P6) — a evidencia fica stale mas o PR nunca e bloqueado."""
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
        await _wait_for_status(handle, {"review_ready"})
        await _wait_until(lambda: state.trigger_preview_calls >= 1,
                          msg="evidencia inicial nunca rodou")

        # refresh 1: dentro do cap
        await handle.signal("refresh_evidence", {})
        await _wait_until(lambda: state.trigger_preview_calls >= 2,
                          msg="refresh 1 nunca rodou")

        # refresh 2: alem do cap -> declinado e auditado, pipeline NAO roda
        await handle.signal("refresh_evidence", {})
        await _wait_until(
            lambda: "evidence_refresh_declined_cap" in read_audit_actions(work_item_id),
            msg="declinio do cap nunca foi auditado")

        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value  # nunca bloqueia (P6)
    assert state.trigger_preview_calls == 2  # initial + 1 refresh; o 2o declinado
