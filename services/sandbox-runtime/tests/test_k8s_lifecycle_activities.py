"""Regressão do modo K8s dos lifecycle activities (plano 09, review adversarial).

Sem cluster: monkeypatcha `activities.select_sandbox_driver` para um driver
stub com `workspace_is_host_visible=False`, exercitando os RAMOS K8s de
provision/checkpoint/rebuild/teardown que os testes Docker/Fake nunca tocam.
Pega, entre outros, o BLOCKER-1 (UnboundLocalError no teardown K8s).
"""
from __future__ import annotations

import asyncio

import pytest
from dse_contracts import CheckpointRef

import sandbox_runtime.activities as activities
from sandbox_runtime.activities import (
    CheckpointSandboxInput,
    ProvisionSandboxInput,
    RebuildSandboxInput,
    TeardownSandboxInput,
    checkpoint_sandbox,
    provision_sandbox,
    rebuild_sandbox,
    teardown_sandbox,
)
from sandbox_runtime.docker_driver import ProvisionedSandbox, ResourceCaps
from sandbox_runtime.driver import SandboxRebuildResult


class _StubK8sDriver:
    """Mesma interface do KubernetesSandboxDriver, sem cluster. Registra as
    chamadas e devolve um ProvisionedSandbox com o nome do Pod."""

    def __init__(self):
        self.provisioned = []
        self.tore_down = []
        self.checkpointed = []

    @property
    def workspace_is_host_visible(self) -> bool:
        return False

    def sandbox_id_for(self, work_item_id: str) -> str:
        return f"dse-sbx-{work_item_id}"

    def _sandbox(self, request) -> ProvisionedSandbox:
        name = self.sandbox_id_for(request.work_item_id)
        return ProvisionedSandbox(
            container_id=name, container_name=name,
            work_item_id=request.work_item_id, tenant_id=request.tenant_id,
            branch=request.branch, workspace_host_path="/workspace",
            checkpoint_bare_repo_path="/checkpoint.git",
            resource_caps=ResourceCaps.from_budget(request.budget), created_new=True,
        )

    def provision(self, request):
        self.provisioned.append(request)
        return self._sandbox(request)

    def checkpoint(self, request) -> CheckpointRef:
        self.checkpointed.append(request)
        return CheckpointRef(work_item_id=request.work_item_id, git_ref="deadbeef", phase=request.phase)

    def rebuild(self, request) -> SandboxRebuildResult:
        return SandboxRebuildResult(sandbox=self._sandbox(request.provision), recovered_sha="cafe1234")

    def teardown(self, sandbox_id: str) -> float:
        self.tore_down.append(sandbox_id)
        return 0.0


@pytest.fixture
def k8s_driver(monkeypatch):
    stub = _StubK8sDriver()
    monkeypatch.setattr(activities, "select_sandbox_driver", lambda: stub)
    # o provision faz validate_runtime_startup(); em dev isso é permissivo
    return stub


def test_provision_k8s_uses_driver_not_docker(k8s_driver, work_item_id, state_dir):
    handle = asyncio.run(provision_sandbox(
        ProvisionSandboxInput(work_item_id=work_item_id, tenant_id="tenant-a",
                              repo="andre2654/fintex-wallet", base_branch="main")))
    assert len(k8s_driver.provisioned) == 1
    # o repo alvo é repassado ao driver (clone in-pod)
    assert k8s_driver.provisioned[0].repo == "andre2654/fintex-wallet"
    assert handle.sandbox_id == f"dse-sbx-{work_item_id}"
    assert handle.container_id == f"dse-sbx-{work_item_id}"


def test_checkpoint_k8s_records_pod_name(k8s_driver, work_item_id, state_dir):
    ref = asyncio.run(checkpoint_sandbox(
        CheckpointSandboxInput(work_item_id=work_item_id, tenant_id="tenant-a", phase="coder")))
    assert ref.git_ref == "deadbeef" and ref.phase == "coder"
    assert len(k8s_driver.checkpointed) == 1


def test_teardown_k8s_completes_without_unbound_local(k8s_driver, work_item_id, state_dir):
    # BLOCKER-1: antes do fix isto levantava UnboundLocalError ('existing').
    asyncio.run(teardown_sandbox(TeardownSandboxInput(work_item_id=work_item_id, tenant_id="tenant-a")))
    assert k8s_driver.tore_down == [f"dse-sbx-{work_item_id}"]


def test_rebuild_k8s_uses_driver(k8s_driver, work_item_id, state_dir):
    handle = asyncio.run(rebuild_sandbox(
        RebuildSandboxInput(work_item_id=work_item_id, tenant_id="tenant-a",
                            checkpoint_ref=CheckpointRef(work_item_id=work_item_id, git_ref="x", phase="coder"))))
    assert handle.sandbox_id == f"dse-sbx-{work_item_id}"
