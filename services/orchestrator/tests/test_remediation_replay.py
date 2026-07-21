"""Remediação — invariante de evolução de payload + determinismo de replay
(spec canônica §5).

Duas garantias que faltavam ao sprint 1 (achado da auditoria pós-S7):

1. **Auto-cura de input a partir do system of record.** Quem inicia o workflow
   pode passar só o `work_item_id` (string) — o caminho `_coerce_input`
   resolve o estado do Postgres via `load_work_item`. Isto é o mesmo mecanismo
   que cura um workflow em voo cujo payload histórico não tem um campo novo:
   o valor vem do banco, não é inventado por default/fixture.

2. **Determinismo de replay.** A história de eventos de uma execução real é
   re-executada com `Replayer` contra a definição ATUAL do workflow. Qualquer
   mudança futura que quebre a determinística (reordenar comandos, remover um
   `patched()`, mudar shape de payload sem compat) falha AQUI — que é
   exatamente o teste que a spec §5 exige ("a replay test using a history
   written by the previous shape"). A história capturada é salva como fixture
   versionada para servir de "shape anterior" na próxima mudança.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Replayer, Worker

from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator.local_activities import LOCAL_ACTIVITIES
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import insert_work_item, new_work_item_id, read_audit_actions
from fakes import FakeControlPlane, build_fake_activities

_FIXTURE_DIR = Path(__file__).parent / "histories"


@pytest.mark.asyncio
async def test_string_start_input_self_heals_from_db(time_skipping_env):
    """Start com `work_item_id` string (não a dataclass): o workflow resolve
    o estado do Postgres em vez de falhar por input incompleto — o mesmo
    caminho que cura payloads históricos sem o campo novo."""
    work_item_id = new_work_item_id("selfheal")
    insert_work_item(work_item_id)
    state = FakeControlPlane()
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"

    async with Worker(
        time_skipping_env.client,
        task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow],
        activities=list(LOCAL_ACTIVITIES) + build_fake_activities(state),
    ):
        # input é a STRING, não WorkItemLifecycleInput — exercita _coerce_input.
        result = await time_skipping_env.client.execute_workflow(
            WorkItemLifecycleWorkflow.run,
            work_item_id,
            id=work_item_id,
            task_queue=task_queue,
        )

    actions = read_audit_actions(work_item_id)
    # Prova de que o ramo string resolveu do banco e o workflow REALMENTE rodou
    # (não poderia auditar intake sem ter carregado o WorkItem do Postgres).
    assert "intake_started" in actions
    # Sem critério de aceite/conteúdo, o gate de completude (S2) estaciona em
    # clarificação — um estado REAL do ciclo de vida, não um erro de input.
    assert result.status in {
        WorkItemStatus.needs_clarification.value,
        WorkItemStatus.escalated.value,
    }


@pytest.mark.asyncio
async def test_workflow_history_replays_deterministically(time_skipping_env):
    """Executa um workflow real, captura sua história de eventos e a re-executa
    com o Replayer contra a definição atual. Falha se houver não-determinismo.
    A história é salva como fixture versionada (shape atual) para a próxima
    mudança replayá-la como 'shape anterior' (spec §5)."""
    work_item_id = new_work_item_id("replay")
    insert_work_item(work_item_id)
    # Caminho curto e determinístico: plano sem expected_files -> escalated,
    # poucas activities, história pequena e estável.
    state = FakeControlPlane(plan_expected_files=[])
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"

    async with Worker(
        time_skipping_env.client,
        task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow],
        activities=list(LOCAL_ACTIVITIES) + build_fake_activities(state),
    ):
        result = await time_skipping_env.client.execute_workflow(
            WorkItemLifecycleWorkflow.run,
            _replay_input(work_item_id),
            id=work_item_id,
            task_queue=task_queue,
        )
        assert result.status == WorkItemStatus.escalated.value
        handle = time_skipping_env.client.get_workflow_handle(work_item_id)
        history = await handle.fetch_history()

    # 1) Replay da história recém-capturada contra a definição ATUAL.
    replayer = Replayer(
        workflows=[WorkItemLifecycleWorkflow],
        data_converter=pydantic_data_converter,
    )
    await replayer.replay_workflow(
        WorkflowHistory.from_json(work_item_id, _history_to_json(history))
    )

    # 2) Persiste a fixture versionada (shape atual) — a próxima mudança de
    # shape replaya ESTA história como o "shape anterior" exigido pela spec §5.
    _FIXTURE_DIR.mkdir(exist_ok=True)
    fixture = _FIXTURE_DIR / "escalated_empty_plan.json"
    if not fixture.exists():
        fixture.write_text(json.dumps(_history_to_json(history), indent=2, sort_keys=True))


@pytest.mark.asyncio
async def test_committed_history_fixture_still_replays(time_skipping_env):
    """Se há uma fixture de história de um shape ANTERIOR versionada, ela deve
    continuar replayável contra a definição atual — a trava de regressão real
    da spec §5. Skip limpo enquanto a fixture ainda não foi capturada."""
    fixture = _FIXTURE_DIR / "escalated_empty_plan.json"
    if not fixture.exists():
        pytest.skip("fixture de história ainda não capturada (roda o teste de replay antes)")
    events = json.loads(fixture.read_text())
    replayer = Replayer(
        workflows=[WorkItemLifecycleWorkflow],
        data_converter=pydantic_data_converter,
    )
    await replayer.replay_workflow(WorkflowHistory.from_json("replay-fixture", events))


def _replay_input(work_item_id: str):
    from dse_orchestrator.models import WorkItemLifecycleInput

    return WorkItemLifecycleInput(
        work_item_id=work_item_id,
        tenant_id="test-tenant",
        requester="usr_test",
        repo="acme/repo",
        base_branch="main",
        acceptance_criteria="criterio verificavel",
        ci_poll_interval_seconds=0.01,
        ci_pending_poll_cap=10,
        activity_retry_cap=2,
    )


def _history_to_json(history) -> dict:
    """`WorkflowHistory.to_json()` não existe em todas as versões; serializa via
    o proto JSON de cada evento, no formato que `WorkflowHistory.from_json`
    aceita ({"events": [...]})."""
    from google.protobuf.json_format import MessageToDict

    return {"events": [MessageToDict(e) for e in history.events]}
