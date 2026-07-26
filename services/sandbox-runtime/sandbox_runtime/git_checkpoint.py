"""Checkpoint/rebuild of the task branch (WSC-E1-T4).

Phase 1 P0: "origin" is a LOCAL bare repo (it does not need to be a real remote —
see CONVENTIONS/the task statement) living in its own directory, bind-mounted
into the sandbox at `/checkpoint.git` (the same path is visible from the host
via `checkpoint_bare_repo_path`, which allows operating either through
`docker exec` or through a plain subprocess on the host against the same
content — both see the same bare repo because it is a bind mount, not a copy).

- `provision_checkpoint_repo`: creates the bare repo (`git init --bare`) +
  installs the scoping `pre-receive` hook (single branch + no force-push).
- `checkpoint`: inside the task workspace, commit (if there are changes) +
  push to the bare repo. Returns the `CheckpointRef` (sha + phase).
- `rebuild_from_checkpoint`: used after `docker kill` (chaos test) — clones the
  bare repo into a fresh workspace and runs `git checkout <sha>` of the task
  branch, proving no commit was lost even with the container killed.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from dse_contracts import CheckpointRef

from .scoped_git import ScopedGitSession, install_pre_receive_guard, write_task_branch_marker


def provision_checkpoint_repo(bare_repo_path: str, branch: str) -> None:
    Path(bare_repo_path).mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--bare", bare_repo_path], check=True, capture_output=True, text=True)
    install_pre_receive_guard(bare_repo_path, branch)


def init_task_workspace(workspace_dir: str, bare_repo_path: str, branch: str, base_branch: str = "main") -> None:
    """Create the task working workspace: a clone of the bare repo (still
    empty on the first run) with the task branch created locally and an
    initial empty commit so that a valid HEAD exists."""
    Path(workspace_dir).mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=workspace_dir, check=True, capture_output=True, text=True)
    subprocess.run(["git", "checkout", "-b", branch], cwd=workspace_dir, check=True, capture_output=True, text=True)
    session = ScopedGitSession(workspace_dir=workspace_dir, branch=branch)
    session.ensure_identity()
    write_task_branch_marker(workspace_dir, branch)  # F6: excluded from the commit/PR
    session.commit(f"chore(dse): initialize the task workspace on branch {branch}")
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
    """Clone the bare repo into a fresh workspace and check out the sha of the
    last checkpoint. Returns the sha actually present at the new workspace's
    HEAD (so the chaos test can compare it against `checkpoint_ref.git_ref`)."""
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
