"""Plano 08 §F (F6): o marcador .dse-task-branch NÃO deve vazar para o commit
(logo, nunca aparece no PR do cliente). Hermético — só git local, sem docker."""
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

    # o marcador existe em disco (resume/checkpoint dependem dele)...
    assert (Path(ws) / ".dse-task-branch").read_text() == branch
    # ...mas o git NUNCA o rastreia (não vaza para o PR) — F6.
    assert ".dse-task-branch" not in _tracked(ws)


def test_real_file_is_committed_but_marker_still_excluded(tmp_path):
    ws = str(tmp_path / "ws")
    bare = str(tmp_path / "bare.git")
    branch = "dse/wi-456"
    provision_checkpoint_repo(bare, branch)
    init_task_workspace(ws, bare, branch)

    # o Coder edita um arquivo real
    (Path(ws) / "app.py").write_text("print('hi')\n")
    session = ScopedGitSession(workspace_dir=ws, branch=branch)
    session.ensure_identity()
    session.commit("feat: app")

    tracked = _tracked(ws)
    assert "app.py" in tracked                      # mudança real vai pro commit
    assert ".dse-task-branch" not in tracked        # marcador continua excluído


def test_write_marker_is_idempotent_on_exclude(tmp_path):
    ws = str(tmp_path / "ws")
    Path(ws, ".git", "info").mkdir(parents=True)
    for _ in range(3):
        write_task_branch_marker(ws, "dse/wi-789")
    exclude = (Path(ws) / ".git" / "info" / "exclude").read_text()
    # uma única entrada, mesmo chamando várias vezes (não duplica)
    assert exclude.split().count(".dse-task-branch") == 1
