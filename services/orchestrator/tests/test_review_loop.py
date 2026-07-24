"""WSB-E3-T4 — loop de review humano: `changes_requested` volta ao Coder no
MESMO branch/PR (nao recria sandbox nem PR), re-valida L1, re-finaliza o
MESMO PR; `approved` so vira Done apos um segundo sinal `merged_by_human`
explicito. Nenhum path deste arquivo (ou do workflow) chama merge — isso e
verificado estaticamente em `test_no_automatic_merge_path`.
"""
from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path

import pytest
from temporalio.worker import Worker

from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator.local_activities import LOCAL_ACTIVITIES
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import insert_work_item, new_work_item_id, read_audit_actions, wait_for_status
from fakes import FakeControlPlane, build_fake_activities


@pytest.mark.asyncio
async def test_changes_requested_cycles_back_to_coder_same_pr(time_skipping_env):
    work_item_id = new_work_item_id("cr")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane()
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
        await wait_for_status(handle, {"review_ready"})
        first_pr_number = (await handle.query(WorkItemLifecycleWorkflow.get_state))["pr_number"]

        await handle.signal("review_comment", {"verdict": "changes_requested", "comment": "ajusta X"})
        await asyncio.sleep(0.2)  # deixa o ciclo de fix rodar
        await wait_for_status(handle, {"review_ready"})

        second_pr_number = (await handle.query(WorkItemLifecycleWorkflow.get_state))["pr_number"]
        assert second_pr_number == first_pr_number  # MESMO PR, nunca recria

        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    assert state.provision_calls == 1  # sandbox nunca reprovisionado
    assert state.finalize_calls == 2  # 1 inicial + 1 re-finalize apos changes_requested
    assert state.coder_turn_calls == 2  # 1 inicial + 1 no ciclo de fix

    actions = read_audit_actions(work_item_id)
    assert "changes_requested" in actions
    assert "coder_fix_applied" in actions
    assert "pr_refinalized" in actions
    assert "merged_by_human" in actions


@pytest.mark.asyncio
async def test_ci_red_triggers_fix_before_waking_human(time_skipping_env):
    work_item_id = new_work_item_id("cired")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(ci_sequence=["red", "green"])
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
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    actions = read_audit_actions(work_item_id)
    assert "ci_red_retrying" in actions
    assert state.coder_turn_calls == 2  # 1 inicial + 1 apos CI red


@pytest.mark.asyncio
async def test_approved_waits_for_explicit_merge_signal(time_skipping_env):
    """Prova que `approved` sozinho NAO basta — o workflow so termina apos
    `merged_by_human` (P3: nenhuma sessao de agente mergeia o proprio
    trabalho; so um humano, via sinal explicito, aciona o Done)."""
    work_item_id = new_work_item_id("waitmerge")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(FakeControlPlane())

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
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})

        # Confirma (via query/audit, NAO via handle.result() com timeout —
        # o ambiente de time-skipping avancaria o relogio virtual ate o
        # default de 10 anos assim que ficasse ocioso esperando so um sinal,
        # o que faria `result()` "completar" prematuramente por timeout de
        # execucao em vez de provar o que queremos) que o workflow esta
        # parado em `approved_awaiting_merge`, sem NENHUM merge automatico.
        actions_before: list[str] = []
        for _ in range(100):
            actions_before = read_audit_actions(work_item_id)
            if "approved_awaiting_merge" in actions_before:
                break
            await asyncio.sleep(0.05)
        assert "approved_awaiting_merge" in actions_before
        assert "merged_by_human" not in actions_before
        assert "done" not in actions_before
        state = await handle.query(WorkItemLifecycleWorkflow.get_state)
        assert state["status"] == "merge_pending"

        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value


def test_no_automatic_merge_path_in_source():
    """Auditavel estaticamente (grep): nenhuma chamada a API de merge do
    GitHub em lugar nenhum do codigo do workflow/worker do WS-B."""
    # Padroes de CHAMADA de verdade (nao prosa em comentario/docstring):
    # `.merge(`, `merge_pull_request(`, ou uma URL de API `/merge` entre aspas.
    src_dir = Path(__file__).resolve().parent.parent / "src" / "dse_orchestrator"
    pattern = re.compile(r"\.merge\s*\(|merge_pull_request\s*\(|[\"']\S*/merge[\"']")
    offenders = []
    for path in src_dir.rglob("*.py"):
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
                continue  # comentarios/docstrings podem falar SOBRE nao mergear
            if pattern.search(line):
                offenders.append(f"{path}:{lineno}: {stripped}")
    assert not offenders, f"encontrada possivel chamada de merge automatico: {offenders}"


