"""WSC-E1-T2: resource caps (--cpus/--memory/--pids-limit) parameterized from
the WorkItem budget + OTel metrics for runtime minutes per resource class."""
from __future__ import annotations

import asyncio

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from dse_contracts.constants import OTEL_ATTR_TENANT, OTEL_ATTR_WORK_ITEM
from sandbox_runtime import metrics
from sandbox_runtime.activities import (
    ProvisionSandboxInput,
    TeardownSandboxInput,
    provision_sandbox,
    teardown_sandbox,
)


def test_resource_caps_applied_from_budget(work_item_id, docker_client, state_dir):
    budget = {"resource_class": "medium", "cpu_limit": 1.5, "memory_mb": 384, "pids_limit": 200}
    inp = ProvisionSandboxInput(work_item_id=work_item_id, tenant_id="tenant-a", budget=budget)
    try:
        handle = asyncio.run(provision_sandbox(inp))
        c = docker_client.containers.get(handle.container_id)
        c.reload()
        host_config = c.attrs["HostConfig"]

        assert host_config["NanoCpus"] == int(1.5 * 1_000_000_000)
        assert host_config["Memory"] == 384 * 1024 * 1024
        assert host_config["PidsLimit"] == 200
    finally:
        asyncio.run(teardown_sandbox(TeardownSandboxInput(work_item_id=work_item_id, tenant_id="tenant-a")))


def test_teardown_emits_runtime_minutes_metric_with_dse_attrs(work_item_id, state_dir):
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    metrics.set_provider_for_test(provider)

    inp = ProvisionSandboxInput(work_item_id=work_item_id, tenant_id="tenant-a", budget={"resource_class": "small"})
    asyncio.run(provision_sandbox(inp))
    asyncio.run(teardown_sandbox(TeardownSandboxInput(work_item_id=work_item_id, tenant_id="tenant-a", stage="coder")))

    data = reader.get_metrics_data()
    assert data is not None
    found = []
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == "dse.sandbox.runtime_minutes":
                    for dp in m.data.data_points:
                        found.append(dict(dp.attributes))

    assert found, "expected at least 1 data point for dse.sandbox.runtime_minutes"
    attrs = found[0]
    assert attrs[OTEL_ATTR_TENANT] == "tenant-a"
    assert attrs[OTEL_ATTR_WORK_ITEM] == work_item_id
    assert attrs["dse.resource_class"] == "small"
