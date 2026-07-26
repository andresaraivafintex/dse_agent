"""Plano 08 §F (F6): the .dse-task-branch marker must NOT leak into the commit
(hence it never shows up in the customer's PR). Hermetic — local git only, no
docker."""
from __future__ import annotations

import subprocess
from pathlib import Path

from sandbox_runtime.git_checkpoint import provision_checkpoint_repo, init_task_workspace
from sandbox_runtime.scoped_git import ScopedGitSession, write_task_branch_marker


def _tracked(workspace_dir: str) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=workspace_dir, capture_output=True, text=True, check=True
    )
    return out.stdout.split()


def test_marker_written_but_not_tracked(tmp_path):
    ws = str(tmp_path / "ws")
    bare = str(tmp_path / "bare.git")
    branch = "dse/wi-123"
    provision_checkpoint_repo(bare, branch)
    init_task_workspace(ws, bare, branch)

    # the marker exists on disk (resume/checkpoint depend on it)...
    assert (Path(ws) / ".dse-task-branch").read_text() == branch
    # ...but git NEVER tracks it (it does not leak into the PR) — F6.
    assert ".dse-task-branch" not in _tracked(ws)


def test_real_file_is_committed_but_marker_still_excluded(tmp_path):
    ws = str(tmp_path / "ws")
    bare = str(tmp_path / "bare.git")
    branch = "dse/wi-456"
    provision_checkpoint_repo(bare, branch)
    init_task_workspace(ws, bare, branch)

    # the Coder edits a real file
    (Path(ws) / "app.py").write_text("print('hi')\n")
    session = ScopedGitSession(workspace_dir=ws, branch=branch)
    session.ensure_identity()
    session.commit("feat: app")

    tracked = _tracked(ws)
    assert "app.py" in tracked                      # the real change makes it into the commit
    assert ".dse-task-branch" not in tracked        # the marker stays excluded


def test_write_marker_is_idempotent_on_exclude(tmp_path):
    ws = str(tmp_path / "ws")
    Path(ws, ".git", "info").mkdir(parents=True)
    for _ in range(3):
        write_task_branch_marker(ws, "dse/wi-789")
    exclude = (Path(ws) / ".git" / "info" / "exclude").read_text()
    # a single entry, even when called several times (no duplication)
    assert exclude.split().count(".dse-task-branch") == 1
