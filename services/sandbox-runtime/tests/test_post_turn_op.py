"""Op post_turn do runner + Activity do Coder em modo K8s (Fase 1, F1.7).

Duas camadas de prova, sem cluster:
  1. A op em si (git real em tmp dirs): prune de descartável, restore de
     lockfile churn, revert de edição de teste e commit/push escopado — a
     MESMA sequência do worker, agora executável dentro do Pod.
  2. `_run_coder_turn_impl` inteiro no modo pod-git: um StubDriver com
     `workspace_is_host_visible=False` roteia execute_op/execute_stage para
     as funções REAIS do runner contra um diretório que simula o volume do
     Pod — o worker nunca toca esse filesystem com git próprio.
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import pytest
from dse_contracts import (
    AgentTurnResult,
    CheckpointOpRequest,
    PostTurnRequest,
    RunCoderTurnInput,
    WorkspaceBootstrapRequest,
)

_RUNNER_DIR = os.path.join(os.path.dirname(__file__), "..", "agent-runner")
sys.path.insert(0, os.path.abspath(_RUNNER_DIR))

from agent_runner.gitops import bootstrap_workspace, checkpoint_workspace  # noqa: E402
from agent_runner.postturn import run_post_turn  # noqa: E402

from sandbox_runtime.activities import _run_coder_turn_impl  # noqa: E402
from sandbox_runtime.driver import StageExecutionResult  # noqa: E402
from sandbox_runtime.remote_substrate import RemoteSubstrate  # noqa: E402


def _bootstrap(tmp_path, wi="wi-pt1"):
    req = WorkspaceBootstrapRequest(
        work_item_id=wi,
        branch=f"dse/{wi}",
        workspace_dir=str(tmp_path / "workspace"),
        checkpoint_path=str(tmp_path / "checkpoint.git"),
    )
    res = bootstrap_workspace(req)
    assert not res.failed
    return req, res


def test_post_turn_full_hygiene_and_scoped_push(tmp_path):
    wi = "wi-pt1"
    req, boot = _bootstrap(tmp_path, wi)
    ws = tmp_path / "workspace"

    # simula o turno do agente: fonte legítima + lixo + churn + edição de teste
    (ws / "src").mkdir()
    (ws / "src" / "app.py").write_text("X = 1\n")
    (ws / "BUG_FIX_REPORT.md").write_text("relatório espontâneo\n")
    (ws / "package-lock.json").write_text('{"lockfileVersion": 3}\n')
    (ws / "tests").mkdir()
    (ws / "tests" / "test_smuggled.py").write_text("def test_x(): pass\n")

    post = run_post_turn(
        PostTurnRequest(
            work_item_id=wi,
            branch=req.branch,
            turn_start_sha=boot.sha,
            commit_message=f"coder({wi}): implementa",
            expected_files=["src/app.py"],
            workspace_dir=str(ws),
        )
    )
    assert not post.failed
    assert post.sha != boot.sha
    assert post.files_changed == ["src/app.py"]
    assert post.pruned == ["BUG_FIX_REPORT.md"]
    assert post.restored_lockfiles == ["package-lock.json"]
    assert post.reverted_tests == ["tests/test_smuggled.py"]
    assert not (ws / "BUG_FIX_REPORT.md").exists()
    assert not (ws / "tests" / "test_smuggled.py").exists()

    # push chegou ao checkpoint (refspec fixo)
    ck = checkpoint_workspace(
        CheckpointOpRequest(work_item_id=wi, branch=req.branch, phase="verify", workspace_dir=str(ws))
    )
    assert ck.sha == post.sha


class PodGitStubDriver:
    """Simula o runtime K8s: o 'volume do Pod' é um tmp dir que o worker NÃO
    opera com git próprio — toda op passa por execute_op/execute_stage e roda
    as funções REAIS do runner contra ele."""

    def __init__(self, pod_workspace: str, pod_checkpoint: str):
        self.pod_workspace = pod_workspace
        self.pod_checkpoint = pod_checkpoint
        self.ops: list[str] = []

    @property
    def supports_isolated_stage_execution(self) -> bool:
        return True

    @property
    def workspace_is_host_visible(self) -> bool:
        return False

    def sandbox_id_for(self, work_item_id: str) -> str:
        return f"pod-{work_item_id}"

    def execute_op(self, sandbox_id, op, payload: dict[str, Any], *, timeout_seconds=180.0):
        self.ops.append(op)
        payload = dict(payload, workspace_dir=self.pod_workspace)
        if op == "checkpoint":
            return checkpoint_workspace(CheckpointOpRequest.model_validate(payload)).model_dump()
        if op == "post_turn":
            return run_post_turn(PostTurnRequest.model_validate(payload)).model_dump()
        raise AssertionError(f"op inesperada: {op}")

    def execute_stage(self, request):
        self.ops.append(f"stage:{request.stage.value}")
        # turno fake "dentro do pod": escreve no volume simulado
        target = os.path.join(self.pod_workspace, "src", "from_pod.py")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as fh:
            fh.write("IN_POD = True\n")
        return StageExecutionResult(
            stage=request.stage,
            output_payload=AgentTurnResult(done=True, cost_usd=0.02).model_dump(),
            exit_code=0,
            duration_seconds=0.01,
        )


def test_coder_activity_full_pipeline_in_pod_git_mode(tmp_path, work_item_id, state_dir):
    branch = f"dse/{work_item_id}"
    pod_ws = tmp_path / "pod-volume" / "workspace"
    pod_ck = tmp_path / "pod-volume" / "checkpoint.git"
    boot = bootstrap_workspace(
        WorkspaceBootstrapRequest(
            work_item_id=work_item_id, branch=branch,
            workspace_dir=str(pod_ws), checkpoint_path=str(pod_ck),
        )
    )
    assert not boot.failed

    driver = PodGitStubDriver(str(pod_ws), str(pod_ck))
    remote = RemoteSubstrate(driver=driver, substrate_name="fake")

    result = asyncio.run(
        _run_coder_turn_impl(
            RunCoderTurnInput(
                work_item_id=work_item_id, tenant_id="tenant-a", instruction="implemente",
            ),
            substrate=remote,
        )
    )

    assert result.files_changed == ["src/from_pod.py"]
    assert result.cost_usd == pytest.approx(0.02)
    # sequência do modo pod-git: sha inicial via checkpoint, turno, pós-turno
    assert driver.ops[0] == "checkpoint"
    assert "stage:coder" in driver.ops
    assert driver.ops[-1] == "post_turn"
