"""Live proof of the full K8s flow on a real cluster (local k3d) — Fase 1.

Exercises the KubernetesSandboxDriver against a real cluster: the hardened Pod
comes up, the bootstrap materializes the git workspace INSIDE the Pod (exec op),
the fake turn runs and edits /workspace, the checkpoint commits and pushes to
/checkpoint.git in-pod, and the teardown removes the Pod. It is the same code
path as the VPS pilot — only the RuntimeClass (gVisor) is left out here (local
k3d does not have it; cfg.runtime_class="" marks weak isolation via annotation,
and the pilotReadiness gate stays closed until the proof on the target cluster).

Skips when there is no kubectl/k3d/active k3d cluster/local image.
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
    )  # idempotent: AlreadyExists is fine
    yield cluster_name


@pytest.fixture()
def k8s_driver(cluster, work_item_id):
    cfg = K8sSandboxConfig(
        namespace=NAMESPACE,
        image=RUNNER_IMAGE,
        runtime_class="",  # local k3d has no gVisor — this proves the FLOW, not the RuntimeClass
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

    # bootstrap already ran inside provision: /workspace is a git repo on the
    # task branch with the initial commit pushed to /checkpoint.git
    head = subprocess.run(
        ["kubectl", "exec", pod, "-n", NAMESPACE, "--", "git", "-C", "/workspace",
         "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == f"dse/{work_item_id}"

    # fake turn INSIDE the Pod
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

    # in-pod checkpoint: commit + push to /checkpoint.git with a fixed refspec
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

    # the bare checkpoint INSIDE the Pod has the sha
    ls = subprocess.run(
        ["kubectl", "exec", pod, "-n", NAMESPACE, "--", "git", "-C", "/checkpoint.git",
         "rev-parse", f"refs/heads/dse/{work_item_id}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert ls == ref.git_ref

    # full in-pod post-turn (--op post_turn): another turn dirties the workspace
    # with junk + a test edit; post_turn prunes, reverts and commits/pushes
    from dse_contracts import PostTurnRequest, PostTurnResult

    k8s_driver.execute_stage(
        StageExecutionRequest(
            sandbox_id=pod,
            work_item_id=work_item_id,
            tenant_id="tenant-k8s",
            stage=Stage.coder,
            input_payload=AgentTurnRequest(
                work_item_id=work_item_id, tenant_id="tenant-k8s", stage="coder",
                substrate="fake", instruction="segundo turno",
                fake_script=[{"write_files": {
                    "src/feature.py": "F = 2\n",
                    "BUG_FIX_REPORT.md": "lixo\n",
                    "tests/test_smuggled.py": "def test_x(): pass\n",
                }, "done": True}],
                gateway={"base_url": "http://model-gateway.dse.svc:4000", "virtual_key": "vk-k8s"},
            ).model_dump(),
            timeout_seconds=120,
        )
    )
    post = PostTurnResult.model_validate(
        k8s_driver.execute_op(
            pod, "post_turn",
            PostTurnRequest(
                work_item_id=work_item_id,
                branch=f"dse/{work_item_id}",
                turn_start_sha=ref.git_ref,
                commit_message=f"coder({work_item_id}): segundo turno",
                expected_files=["src/feature.py"],
            ).model_dump(),
        )
    )
    assert not post.failed
    assert post.files_changed == ["src/feature.py"]
    assert post.pruned == ["BUG_FIX_REPORT.md"]
    assert post.reverted_tests == ["tests/test_smuggled.py"]
    assert post.sha != ref.git_ref


def test_pod_manifest_flags_weak_isolation_without_runtime_class(work_item_id, tmp_path):
    from sandbox_runtime.k8s_driver import build_pod_manifest

    manifest = build_pod_manifest(
        _provision_req(work_item_id, tmp_path),
        K8sSandboxConfig(runtime_class=""),
    )
    assert "runtimeClassName" not in manifest["spec"]
    assert "isolation-warning" in json.dumps(manifest["metadata"]["annotations"])
