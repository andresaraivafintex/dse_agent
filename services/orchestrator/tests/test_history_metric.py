"""Fase 3 — ativacao do alerta de aproximacao do limite de history do Temporal
(infra/ALERTING-RULES.md §3, em conjunto com o WS-F).

O workflow LE `workflow.info().get_current_history_length()/size()` (replay-
safe) e a Activity local `emit_history_metric` EMITE a metrica OTel
`dse.workflow.history_length` (+ size em bytes + contagem de Continue-As-New)
para o collector. Aqui injetamos um InMemoryMetricReader real do SDK OTel no
modulo de metricas (as Activities rodam in-process no Worker de teste) e
provamos que um lifecycle completo emite a metrica com os atributos que a
regra de alerta do WS-F consulta.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from temporalio.worker import Worker

from dse_contracts.constants import OTEL_ATTR_STAGE, OTEL_ATTR_TENANT, OTEL_ATTR_WORK_ITEM
from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator import metrics
from dse_orchestrator.local_activities import LOCAL_ACTIVITIES
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import insert_work_item, new_work_item_id
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


@pytest.mark.asyncio
async def test_full_lifecycle_emits_history_metric_with_work_item_attrs(time_skipping_env):
    reader = InMemoryMetricReader()
    metrics.configure_for_tests(reader)

    work_item_id = new_work_item_id("hist")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(plan_risk_class="low")
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
        await _wait_for_status(handle, {"pr_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value

    points = _collect_points(reader, metrics.METRIC_HISTORY_LENGTH)
    mine = [p for p in points if dict(p.attributes).get(OTEL_ATTR_WORK_ITEM) == work_item_id]
    assert mine, "nenhum data point de history_length com o work_item_id esperado"
    # emitido em mais de uma fronteira: continue_as_new (intake->impl),
    # pr_finalized e cada volta do review loop
    total = sum(p.count for p in mine)
    assert total >= 3
    # history length real e sempre > 0 e cresce ao longo do run
    assert max(p.max for p in mine) > 10
    attrs = dict(mine[0].attributes)
    assert attrs[OTEL_ATTR_TENANT] == "test-tenant"
    assert OTEL_ATTR_STAGE in attrs and metrics.ATTR_CHECKPOINT in attrs

    # contagem de Continue-As-New tambem emitida (>=1: intake->implementacao)
    can_points = _collect_points(reader, metrics.METRIC_CONTINUE_AS_NEW)
    can_mine = [p for p in can_points if dict(p.attributes).get(OTEL_ATTR_WORK_ITEM) == work_item_id]
    assert can_mine and max(p.max for p in can_mine) >= 1
