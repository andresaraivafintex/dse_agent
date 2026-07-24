"""Fase 4 (WS-B wiring) — três entregas do loop hardening & learning:

1. **Merge-base no review loop (WSE-E6-T16).** No caminho changes_requested,
   ANTES de re-rodar o Coder, o workflow chama ACTIVITY_UPDATE_BASE_BRANCH com
   `first_human_review_done=True` (o humano ja revisou -> nunca rebase, so
   merge-base -> zero threads orfas). Conflito nao-resolvivel -> escala a
   humano (nunca resolve a forca, P6). As fakes decodificam o payload com o
   MODEL REAL do contrato (UpdateBaseBranchInput) e retornam
   UpdateBaseBranchResult — se o call site derivar do contrato, quebra aqui.

2. **Episodio de clarificacao (source do WS-C, WSC-E4-T2).** O gate de
   clarificacao emite um skill_episode (source=clarification) na tabela da
   migracao 0019 quando a MESMA lacuna recorre — NENHUMA skill e criada aqui
   (fronteira testada em packages/contracts), so o insumo governavel.

3. **Metrica de qualidade de PR (pilot gate).** rounds de review, contagem de
   changes_requested, tempo ate merge e refreshes de evidencia sao emitidos via
   OTel na fronteira terminal do PR (merge/escalacao).
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from temporalio.worker import Worker

from dse_contracts.constants import OTEL_ATTR_TENANT, OTEL_ATTR_WORK_ITEM
from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator import metrics
from dse_orchestrator.local_activities import LOCAL_ACTIVITIES
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import (
    wait_for_status,
    insert_work_item,
    new_work_item_id,
    read_audit_actions,
    read_skill_episodes,
)
from fakes import FakeControlPlane, build_fake_activities


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


def _collect_points(reader: InMemoryMetricReader, metric_name: str):
    data = reader.get_metrics_data()
    points = []
    if data is None:
        return points
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name == metric_name:
                    points.extend(metric.data.data_points)
    return points


# ---------------------------------------------------------------------------
# 1) Merge-base no caminho changes_requested (WSE-E6-T16)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_changes_requested_runs_merge_base_before_coder(time_skipping_env):
    work_item_id = new_work_item_id("mb")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane()  # base_has_drift=True default -> estrategia merge_base
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _wf_input(work_item_id),
            id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"review_ready"})

        await handle.signal("review_comment", {"verdict": "changes_requested", "comment": "ajusta X"})
        await _wait_until(lambda: state.update_base_calls >= 1,
                          msg="merge-base nunca rodou no changes_requested")
        await wait_for_status(handle, {"review_ready"})

        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    # merge-base rodou exatamente uma vez (um lote de changes_requested)
    assert state.update_base_calls == 1
    # ANTES do Coder: o payload carrega first_human_review_done=True (nunca rebase)
    assert state.last_update_base_payload["first_human_review_done"] is True
    assert state.last_update_base_payload["branch"] == f"dse/{work_item_id}"
    assert state.last_update_base_payload["base_branch"] == "main"

    actions = read_audit_actions(work_item_id)
    assert "base_branch_updated" in actions
    # ordem: merge-base ANTES do fix do Coder (evidencia P8, nao asserção)
    assert actions.index("base_branch_updated") < actions.index("coder_fix_applied")


@pytest.mark.asyncio
async def test_merge_base_conflict_escalates_and_does_not_rerun_coder(time_skipping_env):
    """Conflito nao-resolvivel no merge-base -> escala a humano; o Coder NAO e
    re-executado (P6: nunca resolve a forca, nunca segue adivinhando)."""
    work_item_id = new_work_item_id("mbconf")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(base_conflict=True)
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _wf_input(work_item_id),
            id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "changes_requested", "comment": "rebaseia"})
        result = await handle.result()

    assert result.status == WorkItemStatus.escalated.value
    assert "base_branch_merge_conflict" in (result.detail or "")
    assert state.update_base_calls == 1
    # o Coder do ciclo de fix NUNCA rodou (so o turno inicial da implementacao)
    assert state.coder_turn_calls == 1
    actions = read_audit_actions(work_item_id)
    assert "base_branch_updated" in actions
    assert "coder_fix_applied" not in actions
    assert "escalated" in actions


@pytest.mark.asyncio
async def test_merge_base_orphaned_threads_violation_escalates(time_skipping_env):
    """Invariante de exit da Fase 4: merge-base NUNCA orfana threads. Se o dono
    (WS-E) reportar orphaned_threads>0, o workflow escala (nunca segue com a
    invariante violada)."""
    work_item_id = new_work_item_id("mborph")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(base_orphaned_threads=2)
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _wf_input(work_item_id),
            id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "changes_requested", "comment": "x"})
        result = await handle.result()

    assert result.status == WorkItemStatus.escalated.value
    assert "orphaned_threads" in (result.detail or "")
    assert state.coder_turn_calls == 1  # fix cycle nunca rodou


# ---------------------------------------------------------------------------
# 2) Episodio de clarificacao recorrente (source do WS-C, WSC-E4-T2)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_recurring_clarification_gap_emits_skill_episode(time_skipping_env):
    """A mesma lacuna (base_branch/acceptance_criteria continuam faltando depois
    de ja termos pedido clarificacao) gera um skill_episode source=clarification
    com proveniencia — insumo que o WS-C consome. NENHUMA skill e criada aqui."""
    work_item_id = new_work_item_id("clarep")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(FakeControlPlane())

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo=None, base_branch=None, acceptance_criteria=None,
            clarification_round_cap=3,
            clarification_reminder_hours=1.0,
            clarification_escalation_days=10.0,  # nao deixa o timer de escalacao disparar
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
        # responde sempre so o repo -> base_branch/acceptance_criteria RECORREM faltando
        for _ in range(3):
            await wait_for_status(handle, {"needs_clarification"})
            await handle.signal("clarification_answer", {"repo": "acme/repo"})
        result = await handle.result()

    assert result.status == WorkItemStatus.escalated.value  # cap de rounds

    episodes = read_skill_episodes(work_item_id)
    assert episodes, "nenhum skill_episode gravado para a lacuna recorrente"
    sources = {e[0] for e in episodes}
    assert sources == {"clarification"}
    # pattern_key agrupa os campos recorrentes; proveniencia carrega o contexto
    source, pattern_key, occurrence_n, provenance = episodes[0]
    assert pattern_key.startswith("clarification_missing:")
    assert "base_branch" in pattern_key and "acceptance_criteria" in pattern_key
    assert occurrence_n >= 1
    assert provenance["requester"] == "usr_test"
    assert set(provenance["recurring_missing"]) == {"base_branch", "acceptance_criteria"}

    actions = read_audit_actions(work_item_id)
    assert "skill_episode_recorded" in actions


@pytest.mark.asyncio
async def test_non_recurring_clarification_emits_no_episode(time_skipping_env):
    """A PRIMEIRA lacuna (round inicial, antes de qualquer pedido) NUNCA vira
    episodio — so a RECORRENCIA conta. Uma clarificacao respondida de primeira
    nao gera insumo."""
    work_item_id = new_work_item_id("clarno")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(FakeControlPlane())

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo=None, base_branch=None, acceptance_criteria=None,
            clarification_reminder_hours=0.001, clarification_escalation_days=0.001,
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"needs_clarification"})
        # responde TUDO de uma vez -> completa sem recorrencia
        await handle.signal("clarification_answer",
                            {"repo": "acme/repo", "base_branch": "main", "acceptance_criteria": "X"})
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    assert read_skill_episodes(work_item_id) == []
    assert "skill_episode_recorded" not in read_audit_actions(work_item_id)


# ---------------------------------------------------------------------------
# 3) Metrica de qualidade de PR (pilot gate "PR quality thresholds")
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pr_quality_metric_emitted_on_merge(time_skipping_env):
    reader = InMemoryMetricReader()
    metrics.configure_for_tests(reader)

    work_item_id = new_work_item_id("prq")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(coder_files_changed=["frontend/App.tsx"])  # gera evidencia/refresh
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, _wf_input(work_item_id),
            id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"review_ready"})
        # um round de changes_requested -> conta na taxa + 1 refresh de evidencia
        await handle.signal("review_comment", {"verdict": "changes_requested", "comment": "y"})
        await _wait_until(lambda: state.finalize_calls >= 2, msg="fix cycle nunca completou")
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value

    rounds = _collect_points(reader, metrics.METRIC_PR_REVIEW_ROUNDS)
    mine = [p for p in rounds if dict(p.attributes).get(OTEL_ATTR_WORK_ITEM) == work_item_id]
    assert mine, "nenhum data point de review_rounds para o PR"
    attrs = dict(mine[0].attributes)
    assert attrs[OTEL_ATTR_TENANT] == "test-tenant"
    assert attrs[metrics.ATTR_PR_OUTCOME] == "merged"
    # review_round conta os ciclos de fix (changes_requested/ci-red); aqui 1
    assert max(p.max for p in mine) >= 1

    cr = _collect_points(reader, metrics.METRIC_PR_CHANGES_REQUESTED)
    cr_mine = [p for p in cr if dict(p.attributes).get(OTEL_ATTR_WORK_ITEM) == work_item_id]
    assert cr_mine and max(p.max for p in cr_mine) >= 1

    ttm = _collect_points(reader, metrics.METRIC_PR_TIME_TO_MERGE)
    ttm_mine = [p for p in ttm if dict(p.attributes).get(OTEL_ATTR_WORK_ITEM) == work_item_id]
    assert ttm_mine, "tempo-ate-merge nunca emitido no merge"

    ev = _collect_points(reader, metrics.METRIC_PR_EVIDENCE_REFRESHES)
    ev_mine = [p for p in ev if dict(p.attributes).get(OTEL_ATTR_WORK_ITEM) == work_item_id]
    assert ev_mine  # evidence-consumption (proxy) emitido


@pytest.mark.asyncio
async def test_pr_quality_metric_emitted_on_escalation(time_skipping_env):
    """PRs que nao mergeiam (escalados apos o PR existir) tambem emitem a
    metrica — o pilot gate precisa dos dados dos dois desfechos."""
    reader = InMemoryMetricReader()
    metrics.configure_for_tests(reader)

    work_item_id = new_work_item_id("prqesc")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane()
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run,
            _wf_input(work_item_id, review_round_cap=1, coder_retry_cap=99),
            id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "changes_requested", "comment": "r1"})
        await _wait_until(lambda: state.finalize_calls >= 2, msg="round 1 nunca completou")
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "changes_requested", "comment": "r2"})
        result = await handle.result()

    assert result.status == WorkItemStatus.escalated.value
    rounds = _collect_points(reader, metrics.METRIC_PR_REVIEW_ROUNDS)
    mine = [p for p in rounds if dict(p.attributes).get(OTEL_ATTR_WORK_ITEM) == work_item_id]
    assert mine and dict(mine[0].attributes)[metrics.ATTR_PR_OUTCOME] == "escalated"
