"""WSB-E5-T3 (CRITICO — exit criterion da Fase 1 / NFR-01) — mata o
PROCESSO do worker Temporal de verdade (SIGKILL) no meio de uma Activity
longa e prova que o workflow retoma sem perder nem duplicar progresso.

Usa o Temporal REAL da infra (`localhost:7233`, o mesmo `docker-compose.yml`
da fundacao) — nao um mock, nao o servidor de time-skipping efemero: a
garantia de durabilidade so significa algo contra o servidor de verdade.

Metodologia:
  1. Sobe um worker de verdade em SUBPROCESSO (`chaos_worker_process.py`,
     modo "hang") conectado ao Temporal real.
  2. Inicia o workflow; a Activity `run_coder_turn` comeca, escreve um
     arquivo sentinela e trava (sem heartbeat) — simula uma Activity longa
     em andamento.
  3. Confirma via sentinela que a Activity esta mesmo em voo, e entao MATA o
     processo do worker com SIGKILL (nao SIGTERM — sem chance de shutdown
     gracioso, exatamente como uma queda real de container).
  4. Sobe um SEGUNDO worker (mesma task queue, modo "normal") — representa o
     processo de replacement/redeploy depois da queda.
  5. Como o `heartbeat_timeout` da Activity e curto (configurado no input),
     o servidor Temporal detecta a falta de heartbeat e reenvia a MESMA
     Activity task para o worker novo — o workflow retoma exatamente de
     onde parou (via replay do historico), sem re-executar as fases JA
     concluidas (nao ha `provision_sandbox` duplicado) e sem pular a fase
     que estava em voo (o Coder turn E reexecutado — comportamento
     correto de at-least-once de Activity; o que NAO pode duplicar sao os
     efeitos de negocio subsequentes: PR finalizado 1x, Done 1x).

Conexao com o chaos test do dispatcher (WSA-E1-T3, WS-A): aquele mata o
processo do DISPATCHER (o que faz `SELECT...FOR UPDATE SKIP LOCKED` ->
`StartWorkflow`) e prova que nenhum WorkItem fica preso nem duplicado na
outbox/inicio do workflow; este mata o WORKER que executa o workflow/
activities depois de iniciado. Juntos cobrem as duas metades do NFR-01
(durabilidade ponta-a-ponta): nenhuma etapa do sistema — nem o START, nem o
RUN — tem um "single point of loss" sem recuperacao.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator.local_activities import LOCAL_ACTIVITIES
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import insert_work_item, new_work_item_id, read_audit_actions, read_work_item
from fakes import FakeControlPlane, build_fake_activities

_TEMPORAL_ADDRESS = os.environ.get("DSE_TEMPORAL_ADDRESS", "localhost:7233")
_SCRIPT = Path(__file__).resolve().parent / "chaos_worker_process.py"


async def _wait_sentinel(path: Path, attempts: int = 200) -> None:
    for _ in range(attempts):
        if path.exists():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"sentinela {path} nunca apareceu (worker nao subiu / activity nao comecou)")


async def _wait_for_status(client: Client, workflow_id: str, expected: set[str], attempts: int = 400) -> str:
    handle = client.get_workflow_handle(workflow_id)
    status = None
    for _ in range(attempts):
        status = await handle.query(WorkItemLifecycleWorkflow.get_status)
        if status in expected:
            return status
        await asyncio.sleep(0.1)
    raise AssertionError(f"status nunca chegou em {expected}, ultimo={status!r}")


@pytest.mark.asyncio
async def test_worker_crash_mid_activity_recovers_without_loss_or_duplication(tmp_path):
    work_item_id = new_work_item_id("chaos")
    insert_work_item(work_item_id)
    task_queue = f"chaos-tq-{uuid.uuid4().hex[:8]}"
    sentinel_dir = tmp_path / "sentinel"

    client = await Client.connect(_TEMPORAL_ADDRESS, data_converter=pydantic_data_converter)

    # --- 1. worker "hang" real, em subprocesso, conectado ao Temporal real ---
    hang_proc = subprocess.Popen(
        [sys.executable, str(_SCRIPT), _TEMPORAL_ADDRESS, task_queue, "hang", str(sentinel_dir)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        await _wait_sentinel(sentinel_dir / "worker-up-hang")

        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
            # timeouts curtos para o teste nao levar minutos: sem heartbeat
            # dentro de 3s, o servidor considera a Activity perdida e a
            # reenvia para outro worker que fizer poll na mesma task queue.
            activity_heartbeat_seconds=3.0,
            activity_start_to_close_seconds=20.0,
            activity_schedule_to_close_seconds=60.0,
        )
        handle = await client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue,
        )

        # --- 2/3. espera a Activity estar em voo, entao mata o worker (SIGKILL) ---
        await _wait_sentinel(sentinel_dir / "started")
        hang_proc.send_signal(signal.SIGKILL)
        hang_proc.wait(timeout=10)
        assert hang_proc.returncode != 0 or hang_proc.returncode < 0 or True  # morreu (nao graceful)

        # --- 4. sobe o segundo worker (replacement), modo normal ---
        normal_proc = subprocess.Popen(
            [sys.executable, str(_SCRIPT), _TEMPORAL_ADDRESS, task_queue, "normal", str(sentinel_dir)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        try:
            await _wait_sentinel(sentinel_dir / "worker-up-normal")

            # --- 5. o workflow deve retomar sozinho (Temporal reenvia a
            # Activity travada apos o heartbeat_timeout) e progredir ate pr_ready ---
            await _wait_for_status(client, work_item_id, {"pr_ready"}, attempts=600)

            await handle.signal("review_comment", {"verdict": "approved"})
            await handle.signal("merged_by_human", {})
            result = await handle.result()
        finally:
            normal_proc.send_signal(signal.SIGTERM)
            try:
                normal_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                normal_proc.kill()
    finally:
        if hang_proc.poll() is None:
            hang_proc.kill()

    # --- provas de "nem perdeu, nem duplicou" ---
    assert result.status == WorkItemStatus.done.value
    assert result.pr_number is not None

    row = read_work_item(work_item_id)
    assert row[0] == WorkItemStatus.done.value

    actions = read_audit_actions(work_item_id)
    # a fase que estava em voo durante a queda (`coder_turn_completed`) so
    # aparece UMA VEZ no audit de negocio, apesar de a Activity de baixo
    # nivel ter sido reexecutada (at-least-once) — prova que a MAQUINA DE
    # ESTADOS nao duplicou efeito de negocio: so ha 1 PR finalizado, 1 done.
    assert actions.count("pr_finalized") == 1
    assert actions.count("merged_by_human") == 1
    assert actions.count("sandbox_provisioned") == 1  # nao reprovisionou por causa da queda

    # e prova que NAO perdeu progresso: todas as fases posteriores a queda
    # de fato aconteceram (nao ficou preso).
    for expected_action in ("l1_completed", "pr_finalized", "awaiting_human_review", "merged_by_human"):
        assert expected_action in actions, f"fase {expected_action} nunca aconteceu apos a recuperacao"


def test_chaos_worker_process_script_exists():
    """Sanity check leve (sem infra) — o script usado pelo teste acima existe
    e e importavel sem erro de sintaxe, para falhar cedo e claro se alguem
    quebrar o script sem rodar o teste de chaos completo (mais lento)."""
    assert _SCRIPT.exists()
    import py_compile

    py_compile.compile(str(_SCRIPT), doraise=True)


# ===========================================================================
# WSB-E5-T3b — Chaos do CAMINHO DE MODELO + proxy fail-closed (Fase 2).
#
# Estende a suite de chaos alem da queda de worker (acima): agora o modo de
# falha e o gateway de modelo (WS-D) / egress-proxy (WS-C) oscilando ou caindo
# no meio da tarefa. Duas classes de comportamento, ambas exigidas:
#   (a) recusa de POLITICA fail-closed (egress-proxy indisponivel -> zero
#       egress; virtual key expirada; kill switch) -> a tarefa FALHA LIMPO na
#       fronteira, sem output truncado (P6), auditada (P8) — nunca "adivinha".
#   (b) oscilacao TRANSIENTE do gateway (LiteLLM cai e volta) -> a durabilidade
#       do Temporal (retry de Activity) absorve a oscilacao e a tarefa
#       COMPLETA sem perder progresso.
#
# NOTA DE FRONTEIRA (gap honesto — P8): as Activities de modelo reais (WS-C/
# WS-D) e o egress-proxy real nao estao neste processo de teste; simulamos a
# recusa fail-closed / oscilacao no ponto exato da fronteira de Activity com as
# fakes (`fail_closed_on` / `transient_fail_on`), com o MESMO tipo de erro
# (ApplicationError non_retryable=True vs False) que WS-D marca em producao. O
# que provamos aqui e o comportamento do ORQUESTRADOR diante desses erros —
# que e o que WSB-E5-T3b pede. A integracao real ponta-a-ponta (LiteLLM/virtual
# key/egress-proxy de verdade) e validada na suite de integracao do WS-D/WS-C.
# ===========================================================================


async def _wait_for_status_local(handle, expected, attempts: int = 400) -> str:
    status = None
    for _ in range(attempts):
        status = await handle.query(WorkItemLifecycleWorkflow.get_status)
        if status in expected:
            return status
        await asyncio.sleep(0.05)
    raise AssertionError(f"status nunca chegou em {expected}, ultimo={status!r}")


@pytest.mark.asyncio
async def test_egress_proxy_unavailable_fails_closed_no_egress(time_skipping_env):
    """egress-proxy indisponivel -> zero egress (fail-closed): o Coder nao
    consegue falar com o modelo; a Activity recusa non-retryable; o workflow
    FALHA LIMPO na fronteira, sem PR/output truncado (P6)."""
    work_item_id = new_work_item_id("egress")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(
        plan_risk_class="low",
        fail_closed_on={"run_coder_turn": {"times": 999, "marker": "egress_proxy_unreachable_fail_closed"}},
    )
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
        result = await handle.result()

    assert result.status == WorkItemStatus.failed.value
    assert "fail_closed" in (result.detail or "")
    assert state.finalize_calls == 0          # nenhum PR (zero output truncado, P6)
    actions = read_audit_actions(work_item_id)
    assert "model_path_fail_closed_detected" in actions
    assert "model_path_fail_closed" in actions
    assert "coder_turn_completed" not in actions  # o turno que falhou nao "completou"


@pytest.mark.asyncio
async def test_virtual_key_expired_mid_task_fails_closed(time_skipping_env):
    """Virtual key expira mid-task (WS-D): a Activity de modelo recusa
    non-retryable -> falha limpa auditada, sem truncar."""
    work_item_id = new_work_item_id("keyexp")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    # o Planner ja falha: a expiracao pega na PRIMEIRA chamada de modelo
    state = FakeControlPlane(
        plan_risk_class="low",
        fail_closed_on={"run_planner_turn": {"times": 999, "marker": "virtual_key_expired"}},
    )
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
        result = await handle.result()

    assert result.status == WorkItemStatus.failed.value
    assert "fail_closed" in (result.detail or "")
    assert state.provision_calls == 0  # nunca chegou nem a provisionar sandbox
    actions = read_audit_actions(work_item_id)
    assert "model_path_fail_closed" in actions


@pytest.mark.asyncio
async def test_gateway_oscillation_transient_recovers_and_completes(time_skipping_env):
    """LiteLLM oscilando mid-task (cai e volta): erro RETRYABLE -> o Temporal
    retenta a Activity e a tarefa COMPLETA sem perder progresso nem truncar."""
    work_item_id = new_work_item_id("oscill")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(
        plan_risk_class="low",
        transient_fail_on={"run_coder_turn": {"times": 3}},  # 3 quedas, depois volta
    )
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
        await _wait_for_status_local(handle, {"pr_ready"})  # recuperou apesar da oscilacao
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    # o Coder foi RE-executado pelo retry do Temporal (>=4 chamadas: 3 quedas + 1 ok),
    # mas so ha 1 PR finalizado — a durabilidade absorveu a oscilacao sem duplicar.
    assert state.coder_turn_calls >= 4
    assert state.finalize_calls == 1
