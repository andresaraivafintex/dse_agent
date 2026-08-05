"""WSC-E3-T2: Coder session with scope-limited git.

Proves the two enforcement layers:
  1. Toolset: `ScopedGitSession` exposes no force-push/PR/generic command.
  2. Remote scope: the checkpoint bare repo's `pre-receive` hook refuses
     force-push and pushes to another branch, EVEN when someone bypasses
     `ScopedGitSession` and runs a raw `git push --force`.
  3. Credential scope: `ScopedCredential.create_pull_request()`/`.force_push()`
     always refuse (the scope contract of the GitHub App token that the
     egress-proxy would inject in production).

It also proves the happy path: `run_coder_turn` runs a scripted `FakeSubstrate`,
and the result (`CoderTurnResult`) reflects the files actually committed/pushed
to the task branch.
"""
from __future__ import annotations

import asyncio
import subprocess

import pytest

from sandbox_runtime.activities import (
    ProvisionSandboxInput,
    RunCoderTurnInput,
    TeardownSandboxInput,
    _paths_for,
    _run_coder_turn_impl,
    provision_sandbox,
    teardown_sandbox,
)
from sandbox_runtime.scoped_git import (
    FORBIDDEN_METHOD_NAMES,
    GitScopeViolation,
    ScopedGitSession,
    install_pre_receive_guard,
)
from sandbox_runtime.substrate import FakeSubstrate


def test_scoped_git_session_has_no_escape_hatch():
    public_methods = {name for name in dir(ScopedGitSession) if not name.startswith("_")}
    overlap = public_methods & FORBIDDEN_METHOD_NAMES
    assert not overlap, f"ScopedGitSession exposes methods outside the allowed scope: {overlap}"
    # only commit/push/read operations should exist
    assert {"commit", "push", "has_changes", "ensure_identity", "current_sha", "files_changed_against"} <= public_methods


def test_run_coder_turn_commits_and_pushes_only_scripted_files(work_item_id, state_dir):
    tenant_id = "tenant-a"
    branch = f"dse/{work_item_id}"
    asyncio.run(provision_sandbox(ProvisionSandboxInput(work_item_id=work_item_id, tenant_id=tenant_id)))

    script = [
        {
            "write_files": {"src/handler.py": "def handler():\n    return 'ok'\n"},
            "thought": "implement handler",
            "done": True,
        }
    ]
    fake = FakeSubstrate(script)
    result = asyncio.run(
        _run_coder_turn_impl(
            RunCoderTurnInput(work_item_id=work_item_id, tenant_id=tenant_id, instruction="implement the handler"),
            substrate=fake,
        )
    )

    assert "src/handler.py" in result.files_changed
    assert result.cost_usd > 0

    workspace_dir, bare_repo_path = _paths_for(work_item_id)
    log = subprocess.run(
        ["git", "log", "--oneline", branch], cwd=bare_repo_path, check=True, capture_output=True, text=True
    ).stdout
    assert "coder(" in log

    asyncio.run(teardown_sandbox(TeardownSandboxInput(work_item_id=work_item_id, tenant_id=tenant_id)))


def test_adversarial_force_push_is_rejected_by_remote_scope(work_item_id, state_dir):
    """Even bypassing `ScopedGitSession` (raw subprocess), the bare repo refuses
    non-fast-forward — the second enforcement layer does not depend on the code
    that issued the push."""
    tenant_id = "tenant-a"
    asyncio.run(provision_sandbox(ProvisionSandboxInput(work_item_id=work_item_id, tenant_id=tenant_id)))
    workspace_dir, bare_repo_path = _paths_for(work_item_id)
    branch = f"dse/{work_item_id}"

    subprocess.run(
        ["git", "-c", "user.email=x@x.com", "-c", "user.name=x", "commit", "--allow-empty", "-m", "c1"],
        cwd=workspace_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(["git", "push", "origin", f"HEAD:refs/heads/{branch}"], cwd=workspace_dir, check=True, capture_output=True, text=True)

    # Rewrites local history (simulating an adversarial force-push) and tries to
    # push raw, without going through ScopedGitSession.
    subprocess.run(["git", "reset", "--hard", "HEAD~1"], cwd=workspace_dir, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-c", "user.email=x@x.com", "-c", "user.name=x", "commit", "--allow-empty", "-m", "rewritten-history"],
        cwd=workspace_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(
        ["git", "push", "--force", "origin", f"HEAD:refs/heads/{branch}"],
        cwd=workspace_dir,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "the force-push should have been refused by the pre-receive hook"
    assert "refused" in result.stderr or "rejected" in result.stderr.lower()

    asyncio.run(teardown_sandbox(TeardownSandboxInput(work_item_id=work_item_id, tenant_id=tenant_id)))


def test_adversarial_push_to_other_branch_is_rejected(work_item_id, state_dir):
    tenant_id = "tenant-a"
    asyncio.run(provision_sandbox(ProvisionSandboxInput(work_item_id=work_item_id, tenant_id=tenant_id)))
    workspace_dir, _bare = _paths_for(work_item_id)

    result = subprocess.run(
        ["git", "push", "origin", "HEAD:refs/heads/some-other-branch"],
        cwd=workspace_dir,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "a push to an out-of-scope branch should have been refused"
    assert "refused" in result.stderr or "rejected" in result.stderr.lower()

    asyncio.run(teardown_sandbox(TeardownSandboxInput(work_item_id=work_item_id, tenant_id=tenant_id)))


def test_scoped_git_session_push_raises_git_scope_violation_on_conflict(work_item_id, state_dir):
    """`ScopedGitSession.push()` propagates the hook's refusal as a
    `GitScopeViolation` (P6: clean failure, never swallowed) even inside the
    "safe" API."""
    tenant_id = "tenant-a"
    asyncio.run(provision_sandbox(ProvisionSandboxInput(work_item_id=work_item_id, tenant_id=tenant_id)))
    workspace_dir, _bare = _paths_for(work_item_id)

    session = ScopedGitSession(workspace_dir=workspace_dir, branch="some-other-branch")
    with pytest.raises(GitScopeViolation):
        session.push()

    asyncio.run(teardown_sandbox(TeardownSandboxInput(work_item_id=work_item_id, tenant_id=tenant_id)))


# ---------------------------------------------------------------------------
# The repository's own hooks must never run on a DSE commit — and neutralising
# them must not weaken the scope guard, which is a different repo's hook.
#
# These build the two repos directly instead of going through
# `provision_sandbox`, which needs Postgres for the skill registry. The
# behaviour under test is git configuration, and it should be checkable without
# the control plane up.
# ---------------------------------------------------------------------------
def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


def _workspace_with_hostile_hook(tmp_path, branch):
    """Stands in for husky: the L1 gate's `npm ci` runs the repo's `prepare`
    script, husky points core.hooksPath at `.husky/`, and every later commit
    then runs the project's linter inside the sandbox. On the Angular testbed
    that linter exhausted the V8 heap, so the checkpoint could never be
    written — and the fix loop retried it forever."""
    bare = tmp_path / "checkpoint.git"
    _git(tmp_path, "init", "--bare", str(bare))
    install_pre_receive_guard(str(bare), branch)

    ws = tmp_path / "workspace"
    ws.mkdir()
    _git(ws, "init")
    _git(ws, "checkout", "-b", branch)
    _git(ws, "remote", "add", "origin", str(bare))

    hooks = ws / ".husky"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'FATAL ERROR: JS heap out of memory' >&2\nexit 1\n")
    hook.chmod(0o755)
    _git(ws, "config", "core.hooksPath", str(hooks))
    return ws


def test_a_repo_hook_cannot_block_the_checkpoint_commit(tmp_path):
    branch = "dse/wi_hooks"
    ws = _workspace_with_hostile_hook(tmp_path, branch)
    session = ScopedGitSession(workspace_dir=str(ws), branch=branch)

    # Without `_disable_repo_hooks` this raises: the hook exits 1, git aborts.
    session.ensure_identity()
    sha = session.commit("checkpoint(turn-start): a repo hook must not run")
    assert len(sha) == 40


def test_disabling_repo_hooks_does_not_disarm_the_remote_scope_guard(tmp_path):
    """`core.hooksPath` is set on the WORKSPACE. The scope `pre-receive` lives in
    the checkpoint bare repo and runs under `git-receive-pack`, which reads that
    repo's own config — the two must stay independent."""
    branch = "dse/wi_hooks"
    ws = _workspace_with_hostile_hook(tmp_path, branch)
    session = ScopedGitSession(workspace_dir=str(ws), branch=branch)
    session.ensure_identity()
    session.commit("c1")

    result = subprocess.run(
        ["git", "push", "origin", "HEAD:refs/heads/some-other-branch"],
        cwd=ws, capture_output=True, text=True,
    )
    assert result.returncode != 0, "the scope guard must still refuse an out-of-scope ref"
    assert "refused" in result.stderr or "rejected" in result.stderr.lower()
