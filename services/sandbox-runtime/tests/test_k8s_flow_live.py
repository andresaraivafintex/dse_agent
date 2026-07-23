"""Prova viva do fluxo K8s completo num cluster real (k3d local) — Fase 1.

Exercita o KubernetesSandboxDriver contra um cluster de verdade: Pod
endurecido sobe, o bootstrap materializa o workspace git DENTRO do Pod
(exec op), o turno fake executa e edita o /workspace, o checkpoint commita e
push a para /checkpoint.git in-pod, e o teardown remove o Pod. É o mesmo
caminho de código do piloto na VPS — só o RuntimeClass (gVisor) fica de fora
aqui (k3d local não o tem; cfg.runtime_class="" marca isolamento fraco via
annotation, e o gate pilotReadiness continua fechado até a prova no cluster
alvo).

Pula quando não há kubectl/k3d/cluster k3d ativo/imagem local.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from dse_contracts import AgentTurnRequest, AgentTurnResult, Stage

from sandbox_runtime.driver import SandboxProvisionRequest, StageExecutionRequest
from sandbox_runtime.k8s_driver import K8sSandboxConfig, KubernetesSandboxDriver, pod_name_for

RUNNER_IMAGE = "dse/agent-runner:local"
NAMESPACE = "dse-sbx-proof"


def _current_context() -> str:
    if shutil.which("kubectl") is None:
        return ""
    proc = subprocess.run(
        ["kubectl", "config", "current-context"], capture_output=True, text=True
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _ready() -> bool:
    ctx = _current_context()
    if not ctx.startswith("k3d-") or shutil.which("k3d") is None:
        return False
    if subprocess.run(["kubectl", "get", "nodes"], capture_output=True).returncode != 0:
        return False
    return (
        subprocess.run(
            ["docker", "image", "inspect", RUNNER_IMAGE], capture_output=True
        ).returncode
        == 0
    )


pytestmark = pytest.mark.skipif(
    not _ready(),
    reason="exige kubectl+k3d com contexto k3d ativo e a imagem dse/agent-runner:local",
)


@pytest.fixture(scope="module")
def cluster():
    cluster_name = _current_context().removeprefix("k3d-")
    subprocess.run(
        ["k3d", "image", "import", RUNNER_IMAGE, "-c", cluster_name],
        check=True, capture_output=True, text=True, timeout=300,
    )
    subprocess.run(
        ["kubectl", "create", "namespace", NAMESPACE],
        capture_output=True, text=True,
    )  # idempotente: AlreadyExists é ok
    yield cluster_name


@pytest.fixture()
def k8s_driver(cluster, work_item_id):
    cfg = K8sSandboxConfig(
        namespace=NAMESPACE,
        image=RUNNER_IMAGE,
        runtime_class="",  # k3d local não tem gVisor — prova de FLUXO, não de RuntimeClass
        service_account="default",
    )
    driver = KubernetesSandboxDriver(cfg)
    yield driver
    driver.teardown(pod_name_for(work_item_id))


def _provision_req(work_item_id: str, tmp_path) -> SandboxProvisionRequest:
    return SandboxProvisionRequest(
        work_item_id=work_item_id,
        tenant_id="tenant-k8s",
        branch=f"dse/{work_item_id}",
        workspace_path=str(tmp_path / "unused-host-path"),
        checkpoint_path=str(tmp_path / "unused-host-checkpoint"),
    )


def test_full_pod_flow_provision_bootstrap_turn_checkpoint(k8s_driver, work_item_id, tmp_path):
    sandbox = k8s_driver.provision(_provision_req(work_item_id, tmp_path))
    pod = sandbox.container_name
    assert pod == pod_name_for(work_item_id)

    # bootstrap já rodou dentro do provision: /workspace é um repo git com o
    # branch da tarefa e o commit inicial pushado para /checkpoint.git
    head = subprocess.run(
        ["kubectl", "exec", pod, "-n", NAMESPACE, "--", "git", "-C", "/workspace",
         "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == f"dse/{work_item_id}"

    # turno fake DENTRO do Pod
    turn = AgentTurnRequest(
        work_item_id=work_item_id,
        tenant_id="tenant-k8s",
        stage="coder",
        substrate="fake",
        instruction="prova k8s",
        fake_script=[{"write_files": {"src/pod.py": "IN_POD = True\n"}, "done": True}],
        gateway={"base_url": "http://model-gateway.dse.svc:4000", "virtual_key": "vk-k8s"},
    )
    result = k8s_driver.execute_stage(
        StageExecutionRequest(
            sandbox_id=pod,
            work_item_id=work_item_id,
            tenant_id="tenant-k8s",
            stage=Stage.coder,
            input_payload=turn.model_dump(),
            timeout_seconds=120,
        )
    )
    out = AgentTurnResult.model_validate(result.output_payload)
    assert out.done and not out.failed

    inside = subprocess.run(
        ["kubectl", "exec", pod, "-n", NAMESPACE, "--", "cat", "/workspace/src/pod.py"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert inside == "IN_POD = True\n"

    # checkpoint in-pod: commit + push para /checkpoint.git com refspec fixo
    from sandbox_runtime.driver import SandboxCheckpointRequest

    ref = k8s_driver.checkpoint(
        SandboxCheckpointRequest(
            work_item_id=work_item_id,
            workspace_path="/workspace",
            branch=f"dse/{work_item_id}",
            phase="coder",
        )
    )
    assert ref.git_ref and ref.phase == "coder"

    # o checkpoint bare DENTRO do Pod tem o sha
    ls = subprocess.run(
        ["kubectl", "exec", pod, "-n", NAMESPACE, "--", "git", "-C", "/checkpoint.git",
         "rev-parse", f"refs/heads/dse/{work_item_id}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert ls == ref.git_ref


def test_pod_manifest_flags_weak_isolation_without_runtime_class(work_item_id, tmp_path):
    from sandbox_runtime.k8s_driver import build_pod_manifest

    manifest = build_pod_manifest(
        _provision_req(work_item_id, tmp_path),
        K8sSandboxConfig(runtime_class=""),
    )
    assert "runtimeClassName" not in manifest["spec"]
    assert "isolation-warning" in json.dumps(manifest["metadata"]["annotations"])
