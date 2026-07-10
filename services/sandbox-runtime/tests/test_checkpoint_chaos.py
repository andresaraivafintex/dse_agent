"""WSC-E1-T4: checkpoint = commit+push para um bare repo local de
checkpoint; teste de chaos: mata o container no meio (`docker kill`) e prova
que `rebuild_sandbox` a partir do último checkpoint recupera o estado sem
perder commits."""
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

    # Coder "escreve" um arquivo e comita fora de qualquer checkpoint ainda
    # (simulando trabalho em progresso).
    (Path(workspace_dir) / "feature.py").write_text("def handler():\n    return 42\n")
    _git(["add", "-A"], workspace_dir)
    _git(["-c", "user.email=coder@dse.local", "-c", "user.name=dse-coder", "commit", "-m", "feat: handler"], workspace_dir)

    ref: CheckpointRef = asyncio.run(
        checkpoint_sandbox(CheckpointSandboxInput(work_item_id=work_item_id, tenant_id=tenant_id, phase="mid_task"))
    )
    assert ref.git_ref

    # Prova que o commit está mesmo no bare repo (fonte de verdade durável),
    # não só no workspace que está prestes a morrer com o container.
    _bare_repo_path = _paths_for(work_item_id)[1]
    log_in_bare = subprocess.run(
        ["git", "log", "--oneline", handle.branch], cwd=_bare_repo_path, check=True, capture_output=True, text=True
    ).stdout
    assert "feat: handler" in log_in_bare

    # CHAOS: mata o container no meio da tarefa (sem passar por teardown
    # gracioso) — simula crash de host/OOM/preempção.
    docker_driver.kill_container(handle.container_id)
    docker_client.containers.get(handle.container_id).reload()
    assert docker_client.containers.get(handle.container_id).status in ("exited", "dead")

    # REBUILD: recria o sandbox a partir do último checkpoint.
    new_handle = asyncio.run(
        rebuild_sandbox(
            RebuildSandboxInput(work_item_id=work_item_id, tenant_id=tenant_id, checkpoint_ref=ref, branch=handle.branch)
        )
    )
    assert new_handle.container_id != handle.container_id

    new_workspace_dir, _ = _paths_for(work_item_id)
    new_workspace_dir += "-rebuilt"
    recovered_log = _git(["log", "--oneline"], new_workspace_dir).stdout
    assert "feat: handler" in recovered_log, "commit perdido após rebuild — checkpoint não recuperou o estado"
    assert (Path(new_workspace_dir) / "feature.py").read_text() == "def handler():\n    return 42\n"

    asyncio.run(teardown_sandbox(TeardownSandboxInput(work_item_id=work_item_id, tenant_id=tenant_id)))
