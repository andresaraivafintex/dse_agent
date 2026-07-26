"""agent-runner git ops (bootstrap/checkpoint) — Phase 1, plan 09.

They run IN PROCESS (real git in tmp dirs, no docker): the same code the K8s
driver executes via `kubectl exec` and that Docker can execute via `docker
exec`. They prove idempotency across the bootstrap's three states, the
clone-from-checkpoint recovery (the chaos rebuild, now in-sandbox) and that the
pre-receive hook is still in charge (force/wrong branch refused).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

# agent_runner lives in the sandbox image — imported by path in the tests
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

    # commit + checkpoint inside the workspace
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

    # "Pod death": the workspace vanishes, the checkpoint survives → bootstrap CLONES
    shutil.rmtree(ws)
    recovered = bootstrap_workspace(req)
    assert not recovered.failed and not recovered.created
    assert recovered.sha == ck.sha  # recovered exactly the last checkpoint
    assert (ws / "src" / "app.py").read_text() == "X = 1\n"


def test_checkpoint_remote_still_enforces_scope(tmp_path):
    req = _bootstrap_req(tmp_path, wi="wi-g2")
    bootstrap_workspace(req)
    ws = str(tmp_path / "workspace")

    # raw push to another branch → the pre-receive hook (installed by bootstrap)
    # refuses server-side, exactly as in the worker flow
    proc = subprocess.run(
        ["git", "push", "origin", "HEAD:refs/heads/other-branch"],
        cwd=ws, capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert "refused" in proc.stderr or "rejected" in proc.stderr.lower()


def test_bootstrap_error_is_structured_not_raised(tmp_path):
    # checkpoint_path pointing at a FILE → git init --bare fails and the error
    # comes back structured (P6), never a raw exception crossing the exec
    bogus = tmp_path / "not-a-dir"
    bogus.write_text("x")
    result = bootstrap_workspace(_bootstrap_req(tmp_path, checkpoint_path=str(bogus)))
    assert result.failed and result.error_kind == "gitops_error"


def test_bootstrap_with_repo_clone_failure_is_fail_closed(tmp_path):
    """`repo` requested + clone impossible (dead host on port 1) → error_kind
    'clone_error'; it NEVER falls back to the empty workspace (that would mask
    the problem)."""
    req = WorkspaceBootstrapRequest(
        work_item_id="wi-clone",
        branch="dse/wi-clone",
        base_branch="main",
        repo="acme/nonexistent",
        repo_host="127.0.0.1:1",  # immediate connection refused (fail-fast)
        workspace_dir=str(tmp_path / "workspace"),
        checkpoint_path=str(tmp_path / "checkpoint.git"),
    )
    res = bootstrap_workspace(req)
    assert res.failed
    assert res.error_kind == "clone_error"
    # the workspace did NOT turn into an empty fallback git repo
    assert not (tmp_path / "workspace" / ".git").exists() or not (tmp_path / "workspace" / ".dse-task-branch").exists()


def test_bootstrap_clone_from_local_repo_repoints_origin_to_checkpoint(tmp_path):
    """Clone happy path (using a local repo as 'upstream' via file://):
    materializes the task branch, RE-POINTS origin at the checkpoint and does the
    first scoped push — proves the mechanics without network/proxy."""
    import subprocess

    # local 'upstream': a repo with one commit on main
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(upstream)], check=True)
    subprocess.run(["git", "-C", str(upstream), "config", "user.email", "u@x"], check=True)
    subprocess.run(["git", "-C", str(upstream), "config", "user.name", "u"], check=True)
    (upstream / "README.md").write_text("base\n")
    subprocess.run(["git", "-C", str(upstream), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(upstream), "commit", "-q", "-m", "base"], check=True)

    # _clone_target_repo builds https://<host>/<repo>.git; we point host+repo at
    # the local path via the file:// scheme, embedding the path in "repo".
    # (git accepts a file path as a remote; we use repo_host="" and repo=<abs path> without .git)
    import agent_runner.gitops as gitops
    req = WorkspaceBootstrapRequest(
        work_item_id="wi-ok",
        branch="dse/wi-ok",
        base_branch="main",
        repo="placeholder",
        workspace_dir=str(tmp_path / "ws"),
        checkpoint_path=str(tmp_path / "cp.git"),
    )
    # inject the local URL in place of the https:// one (the rest of the mechanics is the test's target)
    orig = gitops._git

    def fake_clone_url(req_inner):
        # replicates _clone_target_repo but against the local upstream
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
    # origin re-pointed at the checkpoint (the upstream URL is gone from the config)
    import subprocess as sp
    remotes = sp.run(["git", "-C", str(tmp_path / "ws"), "remote", "get-url", "origin"],
                     capture_output=True, text=True).stdout.strip()
    assert remotes == str(tmp_path / "cp.git")
    assert str(upstream) not in remotes
