"""WSC-E1-T4: checkpoint = commit+push to a local bare checkpoint repo; chaos
test: kills the container mid-task (`docker kill`) and proves that
`rebuild_sandbox` from the last checkpoint recovers the state without losing
commits."""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from dse_contracts import CheckpointRef
from sandbox_runtime import docker_driver
from sandbox_runtime.activities import (
    CheckpointSandboxInput,
    ProvisionSandboxInput,
    RebuildSandboxInput,
    TeardownSandboxInput,
    _paths_for,
    checkpoint_sandbox,
    provision_sandbox,
    rebuild_sandbox,
    teardown_sandbox,
)


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_chaos_kill_mid_task_then_rebuild_recovers_commits(work_item_id, docker_client, state_dir):
    tenant_id = "tenant-a"
    provision_in = ProvisionSandboxInput(work_item_id=work_item_id, tenant_id=tenant_id)
    handle = asyncio.run(provision_sandbox(provision_in))

    workspace_dir, _bare = _paths_for(work_item_id)

    # The Coder "writes" a file and commits it outside of any checkpoint yet
    # (simulating work in progress).
    (Path(workspace_dir) / "feature.py").write_text("def handler():\n    return 42\n")
    _git(["add", "-A"], workspace_dir)
    _git(["-c", "user.email=coder@dse.local", "-c", "user.name=dse-coder", "commit", "-m", "feat: handler"], workspace_dir)

    ref: CheckpointRef = asyncio.run(
        checkpoint_sandbox(CheckpointSandboxInput(work_item_id=work_item_id, tenant_id=tenant_id, phase="mid_task"))
    )
    assert ref.git_ref

    # Proves the commit really is in the bare repo (the durable source of
    # truth), not only in the workspace that is about to die with the container.
    _bare_repo_path = _paths_for(work_item_id)[1]
    log_in_bare = subprocess.run(
        ["git", "log", "--oneline", handle.branch], cwd=_bare_repo_path, check=True, capture_output=True, text=True
    ).stdout
    assert "feat: handler" in log_in_bare

    # CHAOS: kills the container mid-task (without going through a graceful
    # teardown) — simulates a host crash/OOM/preemption.
    docker_driver.kill_container(handle.container_id)
    docker_client.containers.get(handle.container_id).reload()
    assert docker_client.containers.get(handle.container_id).status in ("exited", "dead")

    # REBUILD: recreates the sandbox from the last checkpoint.
    new_handle = asyncio.run(
        rebuild_sandbox(
            RebuildSandboxInput(work_item_id=work_item_id, tenant_id=tenant_id, checkpoint_ref=ref, branch=handle.branch)
        )
    )
    assert new_handle.container_id != handle.container_id

    new_workspace_dir, _ = _paths_for(work_item_id)
    new_workspace_dir += "-rebuilt"
    recovered_log = _git(["log", "--oneline"], new_workspace_dir).stdout
    assert "feat: handler" in recovered_log, "commit lost after rebuild — the checkpoint did not restore the state"
    assert (Path(new_workspace_dir) / "feature.py").read_text() == "def handler():\n    return 42\n"

    asyncio.run(teardown_sandbox(TeardownSandboxInput(work_item_id=work_item_id, tenant_id=tenant_id)))
