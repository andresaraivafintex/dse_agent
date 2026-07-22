"""Fase 3 — wiring do pipeline de evidencia (WS-B chama as Activities do
CONTRATO depois de finalize_pr):

  trigger_preview (files_changed do CoderTurnResult, paths-filter FR-20)
    -> se created: run_demo_evidence (base_url do preview; publish interno)
    -> run_visual_diff quando ha midia/screenshot.

Failure mode 9: preview degradado (status "degraded" OU a Activity caindo
inteira) NUNCA bloqueia o PR — evidencia degradada e registrada (audit +
projecao work_item_evidence, migracao 0014) e o fluxo segue para review
humano.

As fakes de WS-E (tests/fakes.py) decodificam cada payload com os models
REAIS do contrato (TriggerPreviewInput/RunDemoEvidenceInput/
RunVisualDiffInput) — payload derivado do contrato quebra AQUI, nao no wire
(licao do adendo 02). Postgres/Temporal reais, nunca mockados.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from temporalio.worker import Worker

from dse_contracts.activities import (
    RunDemoEvidenceInput,
    RunVisualDiffInput,
    TriggerPreviewInput,
)
from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator.local_activities import LOCAL_ACTIVITIES
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import (
    insert_work_item,
    new_work_item_id,
    read_audit_actions,
    read_evidence_row,
)
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


def _wf_input(work_item_id: str, **kw) -> WorkItemLifecycleInput:
    return WorkItemLifecycleInput(
        work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
        repo="acme/repo", base_branch="main", acceptance_criteria="crit", **kw,
    )


@pytest.mark.asyncio
async def test_ui_touching_pr_runs_full_evidence_pipeline(time_skipping_env):
    """PR que toca UI: trigger_preview -> created -> run_demo_evidence com o
    base_url do preview -> run_visual_diff (1o run cria baseline). Payloads
    validados pelos models reais dentro das fakes E re-validados aqui."""
    work_item_id = new_work_item_id("evid")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(plan_risk_class="low",
                             coder_files_changed=["frontend/App.tsx", "api/handler.py"])
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _wf_input(work_item_id),
            id=work_item_id, task_queue=task_queue)
        await _wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    assert state.trigger_preview_calls == 1
    assert state.demo_evidence_calls == 1
    assert state.visual_diff_calls == 1

    # ordem: evidencia SO depois do PR finalizado (nunca bloqueia o finalize)
    log = state.calls_log
    assert log.index("finalize_pr") < log.index("trigger_preview")
    assert log.index("trigger_preview") < log.index("run_demo_evidence")
    assert log.index("run_demo_evidence") < log.index("run_visual_diff")

    # payloads exatos re-validados com os MODELS DO CONTRATO (nao dict leniente)
    prev = TriggerPreviewInput(**state.last_preview_payload)
    assert prev.files_changed == ["frontend/App.tsx", "api/handler.py"]
    assert prev.pr_number == 1000
    demo = RunDemoEvidenceInput(**state.last_demo_payload)
    assert demo.base_url == f"http://preview-{work_item_id}.local"  # URL do preview criado
    vd = RunVisualDiffInput(**state.last_visual_diff_payload)
    assert vd.base_screenshot_key is None  # 1o run -> baseline
    assert vd.candidate_screenshot_path == f"demos/{work_item_id}/screenshot.png"

    actions = read_audit_actions(work_item_id)
    for a in ("preview_triggered", "demo_evidence_completed", "visual_diff_completed"):
        assert a in actions

    row = read_evidence_row(work_item_id)
    assert row is not None
    preview_status, preview_url, demo_passed, video_key, _, baseline_key, refresh_count, reason, detail = row
    assert preview_status == "created"
    assert demo_passed is True
    assert video_key == "evidence/demo.webm"
    assert baseline_key == f"evidence/{work_item_id}/visual.png"
    assert refresh_count == 0 and reason == "initial"


@pytest.mark.asyncio
async def test_docs_only_pr_skips_preview_deterministically(time_skipping_env):
    """FR-20 + §D: PR só de docs (nem UI nem serviço deployável) ->
    skipped_backend_only (paths-filter puro, P1), conta como sucesso, NAO roda
    demo/visual diff e nao bloqueia nada."""
    work_item_id = new_work_item_id("evidskip")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(plan_risk_class="low",
                             coder_files_changed=["docs/x.md", "README.md"])
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _wf_input(work_item_id),
            id=work_item_id, task_queue=task_queue)
        await _wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    assert state.trigger_preview_calls == 1
    assert state.demo_evidence_calls == 0
    assert state.visual_diff_calls == 0
    actions = read_audit_actions(work_item_id)
    assert "evidence_skipped_backend_only" in actions
    row = read_evidence_row(work_item_id)
    assert row is not None and row[0] == "skipped_backend_only"


@pytest.mark.asyncio
async def test_backend_service_pr_now_previews_and_posts_link(time_skipping_env):
    """Plano 08 §D (D1+D2): um PR de serviço backend (.py) NÃO é mais pulado —
    ganha preview (kind=deployable) e o LINK é postado (preview_link_posted no
    ledger). Sem binding no tenant o gate deploys_preview é fail-open."""
    work_item_id = new_work_item_id("evidback")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(plan_risk_class="low",
                             coder_files_changed=["wallet/service.py"])
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _wf_input(work_item_id),
            id=work_item_id, task_queue=task_queue)
        await _wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    assert state.demo_evidence_calls == 1  # backend agora roda o pipeline
    actions = read_audit_actions(work_item_id)
    assert "preview_triggered" in actions
    assert "preview_link_posted" in actions  # D1 — link postado no PR
    row = read_evidence_row(work_item_id)
    assert row is not None and row[0] == "created"


@pytest.mark.asyncio
async def test_degraded_preview_does_not_block_pr(time_skipping_env):
    """Failure mode 9: preview degradado -> evidencia degradada REGISTRADA e o
    fluxo segue para review humano ate Done. O PR nunca e bloqueado."""
    work_item_id = new_work_item_id("eviddeg")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(plan_risk_class="low", preview_mode="degraded",
                             coder_files_changed=["ui/page.tsx"])
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _wf_input(work_item_id),
            id=work_item_id, task_queue=task_queue)
        # chega em pr_ready MESMO com preview degradado
        await _wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value  # nunca Failed por evidencia
    assert state.demo_evidence_calls == 0  # degradado nao tenta demo
    actions = read_audit_actions(work_item_id)
    assert "evidence_degraded" in actions
    assert "pr_finalized" in actions
    row = read_evidence_row(work_item_id)
    assert row is not None and row[0] == "degraded"


@pytest.mark.asyncio
async def test_preview_activity_crash_degrades_not_blocks(time_skipping_env):
    """A Activity de preview caindo INTEIRA (ex.: Argo CD fora do ar) tambem e
    failure mode 9: audita evidence_degraded e segue — nunca propaga a falha
    para o lifecycle do PR."""
    work_item_id = new_work_item_id("evidcrash")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(plan_risk_class="low", preview_mode="raise",
                             coder_files_changed=["ui/page.tsx"])
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _wf_input(work_item_id),
            id=work_item_id, task_queue=task_queue)
        await _wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    actions = read_audit_actions(work_item_id)
    assert "evidence_degraded" in actions
    row = read_evidence_row(work_item_id)
    assert row is not None
    assert row[0] == "degraded" and row[8] == "trigger_preview_failed"
