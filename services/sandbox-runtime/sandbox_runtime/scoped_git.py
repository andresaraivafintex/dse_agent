"""Scope-limited git for the Coder session (WSC-E3-T2).

Two enforcement layers, independent of each other:

1. **Toolset** — `ScopedGitSession` is the ONLY way the sandbox code (the
   `run_coder_turn` Activity, never the LLM/substrate directly — see
   `activities.py`) writes to git. It exposes only `.commit()` and `.push()`;
   `.push()` has the refspec (`HEAD:refs/heads/<branch>`) *hardcoded* — there is
   no parameter to pass `--force` or another branch. There is no escape-hatch
   `run_git_command(*args)` method, and no `open_pull_request()`. The LLM never
   receives a git tool — it only edits files in the workspace (P1: no flow
   decision made by an LLM).

2. **Remote scope (server-side)** — the checkpoint "origin" (a local bare repo
   standing in for the real remote at this phase, see `git_checkpoint.py`) has a
   real `pre-receive` hook installed (`install_pre_receive_guard`) that rejects:
   (a) any ref other than the task's allowed branch, (b) any non-fast-forward
   update (force-push). This holds even if someone bypasses `ScopedGitSession`
   and runs a raw `git push --force` — the hook runs on the "server" side (the
   bare repo), not the client side, so it applies regardless of which code
   performed the push.

   In production (a real push to GitHub through the egress-proxy) the equivalent
   is the scope of the GitHub App token injected by the proxy
   (`egress_proxy.credentials.ScopedCredential`) — the token never carries the
   `pull_requests:write` permission, so a `gh pr create`/`POST /repos/.../pulls`
   attempt made from inside the sandbox fails on the token's own missing
   permission, not on the code's "good will". `ScopedCredential.create_pull_request()`
   (see `egress_proxy/credentials.py`) models exactly that, even in local
   fixture mode.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

PRE_RECEIVE_HOOK_TEMPLATE = """#!/usr/bin/env python3
import sys

ALLOWED_REF = "refs/heads/{branch}"

def main():
    rejected = False
    for line in sys.stdin:
        old_sha, new_sha, refname = line.strip().split()
        if refname != ALLOWED_REF:
            sys.stderr.write(
                f"dse-scope: refused — ref {{refname}} outside the allowed branch "
                f"{{ALLOWED_REF}}\\n"
            )
            rejected = True
            continue
        is_force = (
            old_sha != "0" * 40
            and new_sha != "0" * 40
            and not _is_fast_forward(old_sha, new_sha)
        )
        if is_force:
            sys.stderr.write(
                "dse-scope: refused — non-fast-forward (force-push) blocked "
                "by the task scope\\n"
            )
            rejected = True
    if rejected:
        sys.exit(1)
    sys.exit(0)


def _is_fast_forward(old_sha, new_sha):
    import subprocess as sp

    try:
        merge_base = sp.check_output(
            ["git", "merge-base", "--is-ancestor", old_sha, new_sha]
        )
        return True
    except sp.CalledProcessError:
        return False


if __name__ == "__main__":
    main()
"""


class GitScopeViolation(Exception):
    """Raised when a git operation tries to leave the task's scope."""


def install_pre_receive_guard(bare_repo_path: str, allowed_branch: str) -> None:
    """Install a real `pre-receive` hook in the checkpoint bare repo, rejecting
    pushes outside the task branch or non-fast-forward (force) ones.
    Idempotent — it overwrites any existing hook."""
    hooks_dir = Path(bare_repo_path) / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / "pre-receive"
    hook_path.write_text(PRE_RECEIVE_HOOK_TEMPLATE.format(branch=allowed_branch))
    hook_path.chmod(0o755)


def write_task_branch_marker(workspace_dir: str, branch: str) -> None:
    """plan 08 §F (F6) — writes the `.dse-task-branch` marker (used by
    resume/checkpoint to rediscover the task branch) AND excludes it from EVERY
    commit via `.git/info/exclude`. The marker is DSE infrastructure — it must
    not leak into the customer's PR. Because `commit()` uses `--allow-empty`, the
    initial commit (empty workspace) still creates a valid HEAD even with its
    only file excluded. The exclude is best-effort (if it fails, the marker still
    exists and resume works; only the PR could end up carrying it)."""
    ws = Path(workspace_dir)
    exclude = ws / ".git" / "info" / "exclude"
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text() if exclude.exists() else ""
        if ".dse-task-branch" not in existing.split():
            prefix = existing if existing.endswith("\n") or not existing else existing + "\n"
            exclude.write_text(prefix + ".dse-task-branch\n")
    except OSError:
        pass  # best-effort — never fail the clone/checkpoint because of the exclude
    (ws / ".dse-task-branch").write_text(branch)


@dataclass
class ScopedGitSession:
    """The only git write surface available to the `run_coder_turn` Activity.
    It exposes no force-push, no PR creation, and no generic `run(*args)`."""

    workspace_dir: str
    branch: str
    remote_name: str = "origin"

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=self.workspace_dir,
            check=True,
            capture_output=True,
            text=True,
        )

    def ensure_identity(self, name: str = "dse-coder", email: str = "coder@dse.local") -> None:
        self._run(["config", "user.name", name])
        self._run(["config", "user.email", email])

    def has_changes(self) -> bool:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.workspace_dir,
            capture_output=True,
            text=True,
        )
        return bool(result.stdout.strip())

    def commit(self, message: str) -> str:
        self._run(["add", "-A"])
        self._run(["commit", "-m", message, "--allow-empty"])
        sha = self._run(["rev-parse", "HEAD"]).stdout.strip()
        return sha

    def push(self) -> None:
        """Push hardcoded to `HEAD:refs/heads/<branch>` on the configured remote
        — there is no way to pass `--force` or another refspec through this API.
        Server-side `pre-receive` hook failures propagate as
        `subprocess.CalledProcessError` (P6: clean failure, not swallowed)."""
        try:
            self._run(["push", self.remote_name, f"HEAD:refs/heads/{self.branch}"])
        except subprocess.CalledProcessError as e:
            raise GitScopeViolation(
                f"push refused by the remote (scope): {e.stderr}"
            ) from e

    def current_sha(self) -> str:
        return self._run(["rev-parse", "HEAD"]).stdout.strip()

    def files_changed_against(self, base_sha: str) -> list[str]:
        result = self._run(["diff", "--name-only", base_sha, "HEAD"])
        return [line for line in result.stdout.splitlines() if line]


# "Safe toolset" signature: used by the adversarial test to prove that
# ScopedGitSession's public API contains no force-push / PR / generic-command
# escape hatch.
FORBIDDEN_METHOD_NAMES = {
    "force_push",
    "push_force",
    "create_pull_request",
    "open_pr",
    "run_git_command",
    "run",
    "exec",
}
