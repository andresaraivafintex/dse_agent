"""WSC-E1-T3: a duplicate provision for the same work_item_id does not create
two containers. Calls the real Activities (against real Docker) directly —
nothing here is mocked."""
from __future__ import annotations

import asyncio

import docker
import pytest

from sandbox_runtime import docker_driver
from sandbox_runtime.activities import (
    ProvisionSandboxInput,
    TeardownSandboxInput,
    provision_sandbox,
    teardown_sandbox,
)


@pytest.mark.usefixtures("state_dir")
def test_provision_is_idempotent_by_work_item_id(work_item_id, docker_client):
    inp = ProvisionSandboxInput(
        work_item_id=work_item_id,
        tenant_id="tenant-a",
        budget={"resource_class": "small"},
    )
    try:
        handle1 = asyncio.run(provision_sandbox(inp))
        handle2 = asyncio.run(provision_sandbox(inp))

        assert handle1.container_id == handle2.container_id

        matching = [
            c
            for c in docker_client.containers.list(all=True, filters={"label": f"dse.work_item_id={work_item_id}"})
        ]
        assert len(matching) == 1, f"esperava exatamente 1 container, achou {len(matching)}"
    finally:
        asyncio.run(teardown_sandbox(TeardownSandboxInput(work_item_id=work_item_id, tenant_id="tenant-a")))
        # teardown already removes it; this guarantees cleanup even if the test
        # failed earlier.
        try:
            existing = docker_driver.find_existing_container(work_item_id)
            if existing is not None:
                existing.remove(force=True)
        except docker.errors.NotFound:
            pass
