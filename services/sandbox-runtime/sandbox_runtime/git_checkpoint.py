"""Checkpoint/rebuild do branch da tarefa (WSC-E1-T4).

Fase 1 P0: o "origin" é um bare repo LOCAL (não precisa ser um remoto real —
ver CONVENTIONS/enunciado da tarefa) que vive num diretório próprio,
bind-mountado no sandbox em `/checkpoint.git` (mesmo path visível pelo host
em `checkpoint_bare_repo_path`, o que permite operar tanto via
`docker exec` quanto via subprocess direto no host contra o mesmo conteúdo —
ambos veem o mesmo bare repo porque é um bind mount, não uma cópia).

- `provision_checkpoint_repo`: cria o bare repo (`git init --bare`) + instala
  o hook `pre-receive` de escopo (branch único + no force-push).
- `checkpoint`: dentro do workspace da tarefa, commit (se houver mudanças) +
  push para o bare repo. Retorna o `CheckpointRef` (sha + fase).
- `rebuild_from_checkpoint`: usado após `docker kill` (chaos test) — clona o
  bare repo num workspace novo e faz `git checkout <sha>` do branch da
  tarefa, provando que nenhum commit foi perdido mesmo com o container morto.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from dse_contracts import CheckpointRef

from .scoped_git import ScopedGitSession, install_pre_receive_guard


def provision_checkpoint_repo(bare_repo_path: str, branch: str) -> None:
    Path(bare_repo_path).mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--bare", bare_repo_path], check=True, capture_output=True, text=True)
    install_pre_receive_guard(bare_repo_path, branch)


def init_task_workspace(workspace_dir: str, bare_repo_path: str, branch: str, base_branch: str = "main") -> None:
    """Cria o workspace de trabalho da tarefa: um clone do bare repo (ainda
    vazio na primeira vez) com o branch da tarefa criado localmente e um
    commit inicial vazio para existir um HEAD válido."""
    Path(workspace_dir).mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=workspace_dir, check=True, capture_output=True, text=True)
    subprocess.run(["git", "checkout", "-b", branch], cwd=workspace_dir, check=True, capture_output=True, text=True)
    session = ScopedGitSession(workspace_dir=workspace_dir, branch=branch)
    session.ensure_identity()
    (Path(workspace_dir) / ".dse-task-branch").write_text(branch)
    session.commit(f"chore(dse): inicializa workspace da tarefa no branch {branch}")
    subprocess.run(
        ["git", "remote", "add", "origin", bare_repo_path],
        cwd=workspace_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    session.push()


def checkpoint(work_item_id: str, workspace_dir: str, branch: str, phase: str) -> CheckpointRef:
    session = ScopedGitSession(workspace_dir=workspace_dir, branch=branch)
    session.ensure_identity()
    if session.has_changes():
        session.commit(f"checkpoint({phase}): {work_item_id}")
    session.push()
    sha = session.current_sha()
    return CheckpointRef(work_item_id=work_item_id, git_ref=sha, phase=phase)


def rebuild_from_checkpoint(
    new_workspace_dir: str, bare_repo_path: str, branch: str, checkpoint_ref: CheckpointRef
) -> str:
    """Clona o bare repo num workspace novo e faz checkout do sha do último
    checkpoint. Retorna o sha efetivamente presente no HEAD do novo
    workspace (para o teste de chaos comparar com `checkpoint_ref.git_ref`)."""
    Path(new_workspace_dir).mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--branch", branch, bare_repo_path, new_workspace_dir],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "checkout", checkpoint_ref.git_ref],
        cwd=new_workspace_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=new_workspace_dir, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()
