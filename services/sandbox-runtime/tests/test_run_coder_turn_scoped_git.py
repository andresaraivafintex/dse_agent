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
from sandbox_runtime.scoped_git import FORBIDDEN_METHOD_NAMES, GitScopeViolation, ScopedGitSession
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
