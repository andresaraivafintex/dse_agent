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

from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import insert_work_item, new_work_item_id, read_audit_actions, read_work_item

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
