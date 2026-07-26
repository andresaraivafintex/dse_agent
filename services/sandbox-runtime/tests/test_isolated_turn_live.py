"""LIVE proof of the isolated turn in dev (Fase 1, plano 09 — partial F1.6).

Runs the real agent-runner inside the hardened sandbox container (`docker exec
-i`, image `dse/agent-runner:local` — `make agent-runner-image`) and proves the
two properties the conformance suite merely asserts:

  1. The turn executes INSIDE the sandbox and the worker sees the edit only
     through the workspace bind mount — no SDK in the worker process.
  2. Attempts to write OUTSIDE /workspace fail because of OS isolation
     (read-only rootfs, non-root user), not because of toolset discipline —
     and the denial comes back as a structured result (P6), never a fallback.

These tests ARE SKIPPED when the image does not exist locally (unit CI does not
build it). The equivalent proof under gVisor/K8s still awaits a cluster — the
pilotReadiness.sandboxIsolationVerified gate stays false.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest
from dse_contracts import AgentTurnRequest, AgentTurnResult, Stage

from sandbox_runtime.driver import DockerSandboxDriver, SandboxProvisionRequest, StageExecutionRequest

RUNNER_IMAGE = "dse/agent-runner:local"


def _image_available() -> bool:
    if shutil.which("docker") is None:
        return False
    proc = subprocess.run(
        ["docker", "image", "inspect", RUNNER_IMAGE], capture_output=True, text=True
    )
    return proc.returncode == 0


pytestmark = pytest.mark.skipif(
    not _image_available(),
    reason=f"imagem {RUNNER_IMAGE} ausente — rode `make agent-runner-image`",
)


@pytest.fixture()
def live_sandbox(tmp_path, work_item_id):
    driver = DockerSandboxDriver()
    workspace = tmp_path / "workspace"
    checkpoint = tmp_path / "checkpoint.git"
    workspace.mkdir()
    checkpoint.mkdir()
    sandbox = driver.provision(
        SandboxProvisionRequest(
            work_item_id=work_item_id,
            tenant_id="tenant-live",
            branch=f"dse/{work_item_id}",
            workspace_path=str(workspace),
            checkpoint_path=str(checkpoint),
            image=RUNNER_IMAGE,
        )
    )
    try:
        yield driver, sandbox, workspace
    finally:
        driver.teardown(sandbox.container_id)


def _turn_request(work_item_id: str, fake_script: list[dict]) -> dict:
    return AgentTurnRequest(
        work_item_id=work_item_id,
        tenant_id="tenant-live",
        stage="coder",
        substrate="fake",
        instruction="prova viva",
        fake_script=fake_script,
    # gateway points at the internal network; the fake turn makes no network call
        gateway={"base_url": "http://model-gateway:4000", "virtual_key": "vk-live"},
    ).model_dump()


def test_turn_executes_inside_sandbox_and_edit_arrives_via_bind_mount(live_sandbox, work_item_id):
    driver, sandbox, workspace = live_sandbox
    result = driver.execute_stage(
        StageExecutionRequest(
            sandbox_id=sandbox.container_name,
            work_item_id=work_item_id,
            tenant_id="tenant-live",
            stage=Stage.coder,
            input_payload=_turn_request(
                work_item_id,
                [{"write_files": {"src/inside.py": "WRITTEN_INSIDE = True\n"},
                  "thought": "editado dentro do sandbox", "done": True}],
            ),
            timeout_seconds=120,
        )
    )
    turn = AgentTurnResult.model_validate(result.output_payload)
    assert turn.done and not turn.failed
    # the edit reaches the worker only via the bind mount — the marker exists on the host
    assert (workspace / "src" / "inside.py").read_text() == "WRITTEN_INSIDE = True\n"


def test_escape_attempts_are_denied_by_os_isolation(live_sandbox, work_item_id, tmp_path):
    driver, sandbox, workspace = live_sandbox

    # 1) absolute path outside the workspace → the read-only rootfs denies it (EROFS/EACCES)
    result = driver.execute_stage(
        StageExecutionRequest(
            sandbox_id=sandbox.container_name,
            work_item_id=work_item_id,
            tenant_id="tenant-live",
            stage=Stage.coder,
            input_payload=_turn_request(
                work_item_id, [{"write_files": {"/pwned.txt": "escape"}, "done": True}]
            ),
            timeout_seconds=120,
        )
    )
    turn = AgentTurnResult.model_validate(result.output_payload)
    assert turn.failed and turn.error_kind == "substrate_error"

    # 2) ../ traversal out of the bind mount → the host NEVER sees the file
    driver.execute_stage(
        StageExecutionRequest(
            sandbox_id=sandbox.container_name,
            work_item_id=work_item_id,
            tenant_id="tenant-live",
            stage=Stage.coder,
            input_payload=_turn_request(
                work_item_id, [{"write_files": {"../escape.txt": "escape"}, "done": True}]
            ),
            timeout_seconds=120,
        )
    )
    assert not (tmp_path / "escape.txt").exists()
    assert not (workspace.parent / "escape.txt").exists()
