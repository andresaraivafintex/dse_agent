"""Phase 3 — feeds the alert for approaching Temporal's history limit
(infra/ALERTING-RULES.md §3, together with WS-F).

The workflow READS `workflow.info().get_current_history_length()/size()`
(replay-safe) and the local Activity `emit_history_metric` EMITS the OTel metric
`dse.workflow.history_length` (+ size in bytes + Continue-As-New count) to the
collector. Here we inject a real OTel SDK InMemoryMetricReader into the metrics
module (the Activities run in-process on the test Worker) and prove that a full
lifecycle emits the metric with the attributes WS-F's alert rule queries.
"""
from __future__ import annotations

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

from conftest import insert_work_item, new_work_item_id, wait_for_status
from fakes import FakeControlPlane, build_fake_activities


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
        await wait_for_status(handle, {"review_ready"})
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value

    points = _collect_points(reader, metrics.METRIC_HISTORY_LENGTH)
    mine = [p for p in points if dict(p.attributes).get(OTEL_ATTR_WORK_ITEM) == work_item_id]
    assert mine, "no history_length data point with the expected work_item_id"
    # emitted at more than one boundary: continue_as_new (intake->impl),
    # pr_finalized and every pass of the review loop
    total = sum(p.count for p in mine)
    assert total >= 3
    # the real history length is always > 0 and grows over the run
    assert max(p.max for p in mine) > 10
    attrs = dict(mine[0].attributes)
    assert attrs[OTEL_ATTR_TENANT] == "test-tenant"
    assert OTEL_ATTR_STAGE in attrs and metrics.ATTR_CHECKPOINT in attrs

    # the Continue-As-New count is emitted too (>=1: intake->implementation)
    can_points = _collect_points(reader, metrics.METRIC_CONTINUE_AS_NEW)
    can_mine = [p for p in can_points if dict(p.attributes).get(OTEL_ATTR_WORK_ITEM) == work_item_id]
    assert can_mine and max(p.max for p in can_mine) >= 1
