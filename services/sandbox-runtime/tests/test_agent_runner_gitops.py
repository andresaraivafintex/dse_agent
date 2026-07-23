"""Ops de git do agent-runner (bootstrap/checkpoint) — Fase 1, plano 09.

Rodam EM PROCESSO (git real em tmp dirs, sem docker): o mesmo código que o
driver K8s executa via `kubectl exec` e que o Docker pode executar via
`docker exec`. Provam idempotência nos três estados do bootstrap, a
recuperação clone-do-checkpoint (o rebuild do chaos, agora in-sandbox) e que
o hook pre-receive continua mandando (force/branch errado recusados).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

# agent_runner vive na imagem do sandbox — importado por caminho nos testes
_RUNNER_DIR = os.path.join(os.path.dirname(__file__), "..", "agent-runner")
sys.path.insert(0, os.path.abspath(_RUNNER_DIR))

from agent_runner.gitops import bootstrap_workspace, checkpoint_workspace  # noqa: E402
from dse_contracts import CheckpointOpRequest, WorkspaceBootstrapRequest  # noqa: E402


def _bootstrap_req(tmp_path, wi="wi-g1", **over):
    base = dict(
        work_item_id=wi,
        branch=f"dse/{wi}",
        workspace_dir=str(tmp_path / "workspace"),
        checkpoint_path=str(tmp_path / "checkpoint.git"),
    )
    base.update(over)
    return WorkspaceBootstrapRequest.model_validate(base)


def test_bootstrap_init_then_idempotent_then_recover_from_checkpoint(tmp_path):
    req = _bootstrap_req(tmp_path)

    first = bootstrap_workspace(req)
    assert not first.failed and first.created and first.sha

    again = bootstrap_workspace(req)
    assert not again.failed and not again.created and again.sha == first.sha

    # commit + checkpoint dentro do workspace
    ws = tmp_path / "workspace"
    (ws / "src").mkdir()
    (ws / "src" / "app.py").write_text("X = 1\n")
    ck = checkpoint_workspace(
        CheckpointOpRequest(
            work_item_id="wi-g1", branch=req.branch, phase="coder",
            workspace_dir=str(ws),
        )
    )
    assert not ck.failed and ck.sha != first.sha and ck.phase == "coder"

    # "morte do Pod": workspace some, checkpoint sobrevive → bootstrap CLONA
    shutil.rmtree(ws)
    recovered = bootstrap_workspace(req)
    assert not recovered.failed and not recovered.created
    assert recovered.sha == ck.sha  # recuperou exatamente o último checkpoint
    assert (ws / "src" / "app.py").read_text() == "X = 1\n"


def test_checkpoint_remote_still_enforces_scope(tmp_path):
    req = _bootstrap_req(tmp_path, wi="wi-g2")
    bootstrap_workspace(req)
    ws = str(tmp_path / "workspace")

    # push cru para outro branch → hook pre-receive (instalado pelo bootstrap)
    # recusa server-side, exatamente como no fluxo do worker
    proc = subprocess.run(
        ["git", "push", "origin", "HEAD:refs/heads/outro-branch"],
        cwd=ws, capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert "recusado" in proc.stderr or "rejected" in proc.stderr.lower()


def test_bootstrap_error_is_structured_not_raised(tmp_path):
    # checkpoint_path apontando para um ARQUIVO → git init --bare falha e o
    # erro volta estruturado (P6), nunca exceção crua atravessando o exec
    bogus = tmp_path / "not-a-dir"
    bogus.write_text("x")
    result = bootstrap_workspace(_bootstrap_req(tmp_path, checkpoint_path=str(bogus)))
    assert result.failed and result.error_kind == "gitops_error"
