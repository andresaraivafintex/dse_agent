"""S4 (Phase 5): clone of the REAL target repository into the sandbox workspace.

Architecture decision (risk #3 of the integration plan): the clone happens in
the CONTROL PLANE (the `provision_sandbox` Activity runs in the orchestrator,
which is trusted), using a GitHub App installation token minted here and
IMMEDIATELY stripped from `git config` — the resulting workspace contains the
real CODE, never the token. The sandbox mounts that workspace; the "no
credentials inside the sandbox" invariant (P2/ADR-12) is preserved because the
token only lives in control-plane memory during the clone and is never
persisted.

The production path (egress-proxy injecting the credential at the edge so the
sandbox itself clones) has a known gap in CONNECT-tunnel (Phase 3); this
clone-in-the-control-plane is the honest, explicitly stated PoC path.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

_GITHUB_API = os.environ.get("GITHUB_API_BASE_URL", "https://api.github.com")


class RepoCloneError(RuntimeError):
    pass


def _run(args: list[str], cwd: str | None = None) -> str:
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        # NEVER log the raw command (it may carry the token in the URL) — stderr only.
        raise RepoCloneError(f"git failed (exit={r.returncode}): {r.stderr[-300:]}")
    return r.stdout


def mint_installation_token() -> str | None:
    """Mint a short-lived installation access token from the GitHub App
    credentials in the control-plane environment (GITHUB_APP_ID/PRIVATE_KEY/
    INSTALLATION_ID — the same ones the orchestrator already injects from
    Vault). Returns None when not configured (fixture/test mode)."""
    app_id = os.environ.get("GITHUB_APP_ID")
    private_key = os.environ.get("GITHUB_APP_PRIVATE_KEY")
    installation_id = os.environ.get("GITHUB_APP_INSTALLATION_ID")
    if not (app_id and private_key and installation_id):
        return None
    import jwt  # PyJWT — only when actually configured
    import httpx

    now = int(time.time())
    app_jwt = jwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": app_id},
        private_key, algorithm="RS256",
    )
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(
            f"{_GITHUB_API}/app/installations/{installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {app_jwt}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
        )
        resp.raise_for_status()
        # GitHub returns the installation token under the "token" key.
        return resp.json()["token"]


def clone_repo_into(
    *, workspace_dir: str, repo: str, base_branch: str, task_branch: str,
    bare_repo_path: str, token: str | None,
) -> bool:
    """Clone `github.com/<repo>` (base_branch) into the workspace, create the
    task branch from it, re-point `origin` at the local bare repo (the
    checkpoint destination) and push. Returns True if the real repo was cloned;
    False if there was no token (the caller falls back to the empty-workspace
    init).

    The token appears ONLY in the clone URL and is wiped right after (set-url to
    the bare repo) — verifiable: the workspace's `.git/config` does not contain
    the token."""
    if not token:
        return False
    Path(workspace_dir).parent.mkdir(parents=True, exist_ok=True)
    if Path(workspace_dir).exists():
        # idempotent provisioning: if already cloned, do not redo it
        return True
    host = _GITHUB_API.replace("https://api.", "").replace("/", "") or "github.com"
    if host == "github.com":  # api.github.com -> github.com
        host = "github.com"
    clone_url = f"https://x-access-token:{token}@github.com/{repo}.git"
    from .scoped_git import NO_CUSTOMER_HOOKS  # mesma guarda dos wrappers (#46/hygiene/#52)

    _run(["git", *NO_CUSTOMER_HOOKS, "clone", "--branch", base_branch, "--depth", "50", clone_url, workspace_dir])
    # SCRUB: drop the tokenized URL from the config immediately, pointing origin
    # at the local bare repo (checkpoints). The token persists nowhere.
    _run(["git", "remote", "set-url", "origin", bare_repo_path], cwd=workspace_dir)
    _run(["git", *NO_CUSTOMER_HOOKS, "checkout", "-b", task_branch], cwd=workspace_dir)
    from .scoped_git import ScopedGitSession, write_task_branch_marker  # scope-limited git
    session = ScopedGitSession(workspace_dir=workspace_dir, branch=task_branch)
    session.ensure_identity()
    write_task_branch_marker(workspace_dir, task_branch)  # F6: excluded from the commit
    session.push()
    return True


def token_absent_from_config(workspace_dir: str) -> bool:
    """Proof of the invariant: the token is not in the workspace's git config."""
    cfg = Path(workspace_dir) / ".git" / "config"
    if not cfg.exists():
        return True
    return "x-access-token" not in cfg.read_text()
