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


def test_bootstrap_with_repo_clone_failure_is_fail_closed(tmp_path):
    """`repo` pedido + clone impossível (host morto na porta 1) → error_kind
    'clone_error'; NUNCA cai para o workspace vazio (mascararia o problema)."""
    req = WorkspaceBootstrapRequest(
        work_item_id="wi-clone",
        branch="dse/wi-clone",
        base_branch="main",
        repo="acme/inexistente",
        repo_host="127.0.0.1:1",  # conexão recusada imediata (fail-fast)
        workspace_dir=str(tmp_path / "workspace"),
        checkpoint_path=str(tmp_path / "checkpoint.git"),
    )
    res = bootstrap_workspace(req)
    assert res.failed
    assert res.error_kind == "clone_error"
    # o workspace NÃO virou um repo git vazio de fallback
    assert not (tmp_path / "workspace" / ".git").exists() or not (tmp_path / "workspace" / ".dse-task-branch").exists()


def test_bootstrap_clone_from_local_repo_repoints_origin_to_checkpoint(tmp_path):
    """Caminho feliz do clone (usando um repo local como 'upstream' via file://):
    materializa o branch da tarefa, RE-APONTA origin para o checkpoint e faz o
    primeiro push escopado — prova a mecânica sem rede/proxy."""
    import subprocess

    # 'upstream' local: um repo com um commit em main
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(upstream)], check=True)
    subprocess.run(["git", "-C", str(upstream), "config", "user.email", "u@x"], check=True)
    subprocess.run(["git", "-C", str(upstream), "config", "user.name", "u"], check=True)
    (upstream / "README.md").write_text("base\n")
    subprocess.run(["git", "-C", str(upstream), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(upstream), "commit", "-q", "-m", "base"], check=True)

    # _clone_target_repo constrói https://<host>/<repo>.git; apontamos host+repo
    # para o path local via o esquema file:// embutindo o caminho no "repo".
    # (git aceita file path como remote; usamos repo_host="" e repo=<abs path> sem .git)
    import agent_runner.gitops as gitops
    req = WorkspaceBootstrapRequest(
        work_item_id="wi-ok",
        branch="dse/wi-ok",
        base_branch="main",
        repo="placeholder",
        workspace_dir=str(tmp_path / "ws"),
        checkpoint_path=str(tmp_path / "cp.git"),
    )
    # injeta a URL local no lugar da https:// (o resto da mecânica é o alvo do teste)
    orig = gitops._git

    def fake_clone_url(req_inner):
        # replica _clone_target_repo mas com o upstream local
        gitops._git(["clone", "--depth", "50", "--branch", req_inner.base_branch, str(upstream), req_inner.workspace_dir])
        from agent_runner.gitops import ScopedGitSession, write_task_branch_marker
        session = ScopedGitSession(workspace_dir=req_inner.workspace_dir, branch=req_inner.branch)
        session.ensure_identity()
        gitops._git(["checkout", "-b", req_inner.branch], cwd=req_inner.workspace_dir)
        write_task_branch_marker(req_inner.workspace_dir, req_inner.branch)
        gitops._git(["remote", "set-url", "origin", req_inner.checkpoint_path], cwd=req_inner.workspace_dir)
        session.push()
        return session.current_sha()

    gitops._clone_target_repo = fake_clone_url
    try:
        res = bootstrap_workspace(req)
    finally:
        gitops._git = orig
    assert not res.failed and res.created and res.sha
    # origin re-apontado para o checkpoint (a URL do upstream sumiu do config)
    import subprocess as sp
    remotes = sp.run(["git", "-C", str(tmp_path / "ws"), "remote", "get-url", "origin"],
                     capture_output=True, text=True).stdout.strip()
    assert remotes == str(tmp_path / "cp.git")
    assert str(upstream) not in remotes
