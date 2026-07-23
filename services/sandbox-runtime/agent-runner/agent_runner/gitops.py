"""Ops de git executadas DENTRO do sandbox (runner `--op bootstrap|checkpoint`).

No Docker o worker ainda pode operar o git pelo bind mount; no K8s o workspace
é um volume do Pod e TODA operação de git acontece aqui, via exec. As duas
rotas usam a MESMA disciplina do worker (`scoped_git`): refspec fixo
`HEAD:refs/heads/<branch>`, jamais force, e o hook `pre-receive` de escopo
instalado no bare repo de checkpoint ANTES do primeiro push — o enforcement
mora no remoto, não na boa vontade de quem chama.

Fonte única de verdade: `scoped_git.py` do sandbox_runtime é vendorado na
imagem em build (`_scoped_git.py`); em dev/teste o import resolve direto do
pacote instalado no venv.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from dse_contracts import (
    CheckpointOpRequest,
    CheckpointOpResult,
    WorkspaceBootstrapRequest,
    WorkspaceBootstrapResult,
)

try:  # dev/test: pacote do worker no venv
    from sandbox_runtime.scoped_git import (
        ScopedGitSession,
        install_pre_receive_guard,
        write_task_branch_marker,
    )
except ImportError:  # imagem: cópia vendorada no build (Dockerfile)
    from ._scoped_git import (  # type: ignore[no-redef]
        ScopedGitSession,
        install_pre_receive_guard,
        write_task_branch_marker,
    )


def _git(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} falhou: {proc.stderr.strip()[:300]}")
    return proc


def _ensure_safe_directory() -> None:
    """Bind mounts chegam com dono != uid do sandbox; sem isto o git recusa
    operar ('dubious ownership'). Escopo: só o processo efêmero do exec."""
    subprocess.run(
        ["git", "config", "--global", "--add", "safe.directory", "*"],
        capture_output=True, text=True,
    )


def _is_git_workspace(workspace_dir: str) -> bool:
    return (Path(workspace_dir) / ".git").exists()


def _checkpoint_has_branch(checkpoint_path: str, branch: str) -> bool:
    if not (Path(checkpoint_path) / "HEAD").is_file():
        return False
    proc = subprocess.run(
        ["git", "ls-remote", "--heads", checkpoint_path, branch],
        capture_output=True, text=True, timeout=60,
    )
    return proc.returncode == 0 and bool(proc.stdout.strip())


def bootstrap_workspace(req: WorkspaceBootstrapRequest) -> WorkspaceBootstrapResult:
    """Idempotente nos três estados possíveis do runtime:
      1. workspace já é repo git → devolve o HEAD (retomada pós-restart);
      2. checkpoint já tem o branch → clone (+checkout) — é o rebuild do
         chaos test, agora DENTRO do sandbox;
      3. nada existe → init do zero (espelha `git_checkpoint.init_task_workspace`).
    """
    try:
        _ensure_safe_directory()
        ws = Path(req.workspace_dir)
        ws.mkdir(parents=True, exist_ok=True)

        if req.provision_checkpoint and not (Path(req.checkpoint_path) / "HEAD").is_file():
            Path(req.checkpoint_path).mkdir(parents=True, exist_ok=True)
            _git(["init", "--bare", req.checkpoint_path])
        # hook SEMPRE (re)instalado antes de qualquer push — idempotente
        install_pre_receive_guard(req.checkpoint_path, req.branch)

        if _is_git_workspace(req.workspace_dir):
            sha = _git(["rev-parse", "HEAD"], cwd=req.workspace_dir).stdout.strip()
            return WorkspaceBootstrapResult(sha=sha, created=False)

        if _checkpoint_has_branch(req.checkpoint_path, req.branch):
            _git(["clone", "--branch", req.branch, req.checkpoint_path, req.workspace_dir])
            write_task_branch_marker(req.workspace_dir, req.branch)
            sha = _git(["rev-parse", "HEAD"], cwd=req.workspace_dir).stdout.strip()
            return WorkspaceBootstrapResult(sha=sha, created=False)

        _git(["init"], cwd=req.workspace_dir)
        _git(["checkout", "-b", req.branch], cwd=req.workspace_dir)
        session = ScopedGitSession(workspace_dir=req.workspace_dir, branch=req.branch)
        session.ensure_identity()
        write_task_branch_marker(req.workspace_dir, req.branch)
        session.commit(f"chore(dse): inicializa workspace da tarefa no branch {req.branch}")
        _git(["remote", "add", "origin", req.checkpoint_path], cwd=req.workspace_dir)
        session.push()
        return WorkspaceBootstrapResult(sha=session.current_sha(), created=True)
    except Exception as exc:  # noqa: BLE001 — P6: resultado estruturado
        return WorkspaceBootstrapResult(
            error=f"{type(exc).__name__}: {str(exc)[:400]}", error_kind="gitops_error"
        )


def checkpoint_workspace(req: CheckpointOpRequest) -> CheckpointOpResult:
    try:
        _ensure_safe_directory()
        session = ScopedGitSession(workspace_dir=req.workspace_dir, branch=req.branch)
        session.ensure_identity()
        if session.has_changes():
            session.commit(f"checkpoint({req.phase}): {req.work_item_id}")
        session.push()
        return CheckpointOpResult(sha=session.current_sha(), phase=req.phase)
    except Exception as exc:  # noqa: BLE001 — P6 (inclui GitScopeViolation do hook)
        return CheckpointOpResult(
            phase=req.phase,
            error=f"{type(exc).__name__}: {str(exc)[:400]}",
            error_kind="gitops_error",
        )


__all__ = ["bootstrap_workspace", "checkpoint_workspace"]
