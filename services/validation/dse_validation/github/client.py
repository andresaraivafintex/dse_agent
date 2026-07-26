"""Minimal GitHub REST API client used by the PR finalizer (WSE-E3), the mutable
comment backend (WSE-E3-T7) and the CI status consumption (WSE-E4-T9a).

Two implementations of the same `GitHubClient` `Protocol`:

  - `RealGitHubClient` — real HTTP calls to the GitHub API, authenticated as a
    GitHub App (via `app_auth.py`). This is what runs in production.
  - `FakeGitHubClient` — in-memory fixture, used in all of this workstream's tests
    because no real GitHub App is registered in this session (real
    `GITHUB_APP_ID`/`GITHUB_APP_PRIVATE_KEY`/`GITHUB_APP_INSTALLATION_ID` are
    missing — see README). It implements the SAME interface, so the PR finalizer's
    idempotency logic really is tested; only the HTTP transport is swapped out.

`build_github_client()` picks automatically: real if the 3 GitHub App env vars are
present, fake otherwise (never fails silently — it logs which mode it chose).
"""
from __future__ import annotations

import itertools
import logging
import time
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from dse_validation.config import GitHubConfig
from dse_validation.github.app_auth import fetch_installation_token

logger = logging.getLogger("dse_validation.github")


class GitHubClient(Protocol):
    def get_open_pr_for_branch(self, repo: str, branch: str) -> dict | None: ...

    def get_pull_request(self, repo: str, pr_number: int) -> dict | None: ...

    def create_pr(self, repo: str, head: str, base: str, title: str, body: str) -> dict: ...

    def update_pull_request(self, repo: str, pr_number: int, *, body: str) -> None: ...

    def post_issue_comment(self, repo: str, issue_number: int, body: str) -> str: ...

    def edit_issue_comment(self, repo: str, comment_id: str, body: str) -> None: ...

    def list_check_runs(self, repo: str, ref: str) -> list[dict]: ...

    def rerequest_check_run(self, repo: str, check_run_id: int) -> bool:
        """Phase 3 (WSE-E4-T9b) — targeted re-run of ONE check-run (fix commit).
        `POST /repos/{repo}/check-runs/{id}/rerequest`. Returns False when the
        repo's CI does NOT support per-job re-run (403/422 from the API) — the
        caller records it and moves on (never blocks because of it)."""
        ...

    def list_review_threads(self, repo: str, pr_number: int) -> list[dict]:
        """Phase 4 (WSE-E6-T16) — the PR's review threads, each ANCHORED to a
        commit. `GET /repos/{repo}/pulls/{n}/comments` (review comments). Returns
        `[{id, commit_id, original_commit_id, path}]`. `original_commit_id` is the
        sha the thread was anchored to — that is the one rebase+force-push would
        orphan (merge-base preserves it). Used by the Activity wrapper to know
        which shas must NOT disappear from the branch's history."""
        ...

    def authenticated_remote_url(self, repo: str) -> str:
        """https://x-access-token:<token>@github.com/<repo>.git URL for a git push
        authenticated as the GitHub App, without exposing the token in any logged argv."""
        ...


# ---------------------------------------------------------------------------
# Real
# ---------------------------------------------------------------------------
class RealGitHubClient:
    def __init__(self, cfg: GitHubConfig):
        self._cfg = cfg
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    def _installation_token(self) -> str:
        # 60s margin before the declared expiration (1h) to avoid a race.
        if self._token is None or time.time() >= self._token_expires_at - 60:
            self._token = fetch_installation_token(
                self._cfg.app_id, self._cfg.private_key_pem, self._cfg.installation_id, self._cfg.api_base_url
            )
            self._token_expires_at = time.time() + 3600
        return self._token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._installation_token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def get_open_pr_for_branch(self, repo: str, branch: str) -> dict | None:
        owner = repo.split("/")[0]
        resp = httpx.get(
            f"{self._cfg.api_base_url}/repos/{repo}/pulls",
            headers=self._headers(),
            params={"head": f"{owner}:{branch}", "state": "open"},
            timeout=15.0,
        )
        resp.raise_for_status()
        items = resp.json()
        if not items:
            return None
        pr = items[0]
        return {"number": pr["number"], "html_url": pr["html_url"], "state": pr["state"]}

    def get_pull_request(self, repo: str, pr_number: int) -> dict | None:
        """plano 08 §F (F1) — the PR's REAL state at the source (GitHub), so a
        merge signal is verified against the truth and not just against the
        envelope. Returns merged/merged_by/merge_commit_sha/head_sha; None if the
        PR does not exist."""
        resp = httpx.get(
            f"{self._cfg.api_base_url}/repos/{repo}/pulls/{pr_number}",
            headers=self._headers(),
            timeout=15.0,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        pr = resp.json()
        return {
            "number": pr["number"],
            "state": pr["state"],
            "merged": bool(pr.get("merged")),
            "merged_by": (pr.get("merged_by") or {}).get("login"),
            "merge_commit_sha": pr.get("merge_commit_sha"),
            "head_sha": (pr.get("head") or {}).get("sha"),
            "base_ref": (pr.get("base") or {}).get("ref"),
            "body": pr.get("body") or "",
        }

    def get_tree_paths(self, repo: str, ref: str, *, limit: int = 300) -> list[str]:
        """The repo's file tree at `ref` (found during the real run: the Planner
        proposes expected_files BEFORE the clone — without the tree it guesses
        paths and plan_compliance rejects the real diff). Blob paths only, truncated."""
        resp = httpx.get(
            f"{self._cfg.api_base_url}/repos/{repo}/git/trees/{ref}",
            headers=self._headers(),
            params={"recursive": "1"},
            timeout=20.0,
        )
        resp.raise_for_status()
        tree = resp.json().get("tree", [])
        return [t["path"] for t in tree if t.get("type") == "blob"][:limit]

    def create_pr(self, repo: str, head: str, base: str, title: str, body: str) -> dict:
        resp = httpx.post(
            f"{self._cfg.api_base_url}/repos/{repo}/pulls",
            headers=self._headers(),
            json={"title": title, "head": head, "base": base, "body": body},
            timeout=15.0,
        )
        resp.raise_for_status()
        pr = resp.json()
        return {"number": pr["number"], "html_url": pr["html_url"], "state": pr["state"]}

    def update_pull_request(self, repo: str, pr_number: int, *, body: str) -> None:
        """Edits the PR's BODY (D1: the preview link goes in the description, not
        as a comment). `PATCH /repos/{repo}/pulls/{n}`."""
        resp = httpx.patch(
            f"{self._cfg.api_base_url}/repos/{repo}/pulls/{pr_number}",
            headers=self._headers(),
            json={"body": body},
            timeout=15.0,
        )
        resp.raise_for_status()

    def post_issue_comment(self, repo: str, issue_number: int, body: str) -> str:
        resp = httpx.post(
            f"{self._cfg.api_base_url}/repos/{repo}/issues/{issue_number}/comments",
            headers=self._headers(),
            json={"body": body},
            timeout=15.0,
        )
        resp.raise_for_status()
        return str(resp.json()["id"])

    def edit_issue_comment(self, repo: str, comment_id: str, body: str) -> None:
        resp = httpx.patch(
            f"{self._cfg.api_base_url}/repos/{repo}/issues/comments/{comment_id}",
            headers=self._headers(),
            json={"body": body},
            timeout=15.0,
        )
        resp.raise_for_status()

    def list_issue_comments(self, repo: str, issue_number: int) -> list[dict]:
        """Comments on an issue/PR (a PR is an issue on GitHub) — used for the
        preview link's idempotency (edit instead of duplicating)."""
        resp = httpx.get(
            f"{self._cfg.api_base_url}/repos/{repo}/issues/{issue_number}/comments",
            headers=self._headers(),
            params={"per_page": 100},
            timeout=15.0,
        )
        resp.raise_for_status()
        return [{"id": str(c["id"]), "body": c.get("body") or ""} for c in resp.json()]

    def list_check_runs(self, repo: str, ref: str) -> list[dict]:
        resp = httpx.get(
            f"{self._cfg.api_base_url}/repos/{repo}/commits/{ref}/check-runs",
            headers=self._headers(),
            timeout=15.0,
        )
        resp.raise_for_status()
        return resp.json().get("check_runs", [])

    def rerequest_check_run(self, repo: str, check_run_id: int) -> bool:
        resp = httpx.post(
            f"{self._cfg.api_base_url}/repos/{repo}/check-runs/{check_run_id}/rerequest",
            headers=self._headers(),
            timeout=15.0,
        )
        if resp.status_code in (403, 404, 422):
            # the repo's CI does not support per-job re-run (or the run is not re-runnable).
            return False
        resp.raise_for_status()
        return True

    def list_review_threads(self, repo: str, pr_number: int) -> list[dict]:
        resp = httpx.get(
            f"{self._cfg.api_base_url}/repos/{repo}/pulls/{pr_number}/comments",
            headers=self._headers(),
            params={"per_page": 100},
            timeout=15.0,
        )
        resp.raise_for_status()
        return [
            {
                "id": c.get("id"),
                "commit_id": c.get("commit_id"),
                "original_commit_id": c.get("original_commit_id"),
                "path": c.get("path"),
            }
            for c in resp.json()
        ]

    def authenticated_remote_url(self, repo: str) -> str:
        token = self._installation_token()
        return f"https://x-access-token:{token}@github.com/{repo}.git"


# ---------------------------------------------------------------------------
# Fake / local mode — no real GitHub App. Documented as a fixture in the README.
# ---------------------------------------------------------------------------
@dataclass
class FakeGitHubClient:
    """In-memory state that mimics GitHub just enough to test the PR finalizer's
    idempotency logic, the comment backend and the CI status consumption WITHOUT
    network/real credentials. Each instance represents "GitHub's state"
    persistently across calls (unlike our Postgres, which can be "forgotten" to
    simulate a crash)."""

    _prs: dict[tuple[str, str], dict] = field(default_factory=dict)  # (repo, branch) -> pr dict
    _pr_by_number: dict[tuple[str, int], dict] = field(default_factory=dict)
    _comments: dict[str, str] = field(default_factory=dict)  # comment_id -> body
    _check_runs: dict[tuple[str, str], list[dict]] = field(default_factory=dict)
    _comment_id_seq: itertools.count = field(default_factory=lambda: itertools.count(1))
    _pr_number_seq: itertools.count = field(default_factory=lambda: itertools.count(100))
    create_pr_calls: int = 0

    def get_open_pr_for_branch(self, repo: str, branch: str) -> dict | None:
        pr = self._prs.get((repo, branch))
        if pr is None or pr["state"] != "open":
            return None
        return dict(pr)

    def create_pr(self, repo: str, head: str, base: str, title: str, body: str) -> dict:
        self.create_pr_calls += 1
        number = next(self._pr_number_seq)
        pr = {
            "number": number,
            "html_url": f"https://github.com/{repo}/pull/{number}",
            "state": "open",
            "title": title,
            "body": body,
            "base": base,
        }
        self._prs[(repo, head)] = pr
        self._pr_by_number[(repo, number)] = pr
        return dict(pr)

    def update_pull_request(self, repo: str, pr_number: int, *, body: str) -> None:
        pr = self._pr_by_number.get((repo, pr_number))
        if pr is None:
            raise KeyError(f"PR {pr_number} does not exist in FakeGitHubClient")
        pr["body"] = body

    def get_pull_request(self, repo: str, pr_number: int) -> dict | None:
        pr = self._pr_by_number.get((repo, pr_number))
        if pr is None:
            return None
        return {"number": pr["number"], "state": pr["state"], "body": pr.get("body") or ""}

    def post_issue_comment(self, repo: str, issue_number: int, body: str) -> str:
        comment_id = str(next(self._comment_id_seq))
        self._comments[comment_id] = body
        return comment_id

    def edit_issue_comment(self, repo: str, comment_id: str, body: str) -> None:
        if comment_id not in self._comments:
            raise KeyError(f"comment {comment_id} does not exist in FakeGitHubClient")
        self._comments[comment_id] = body

    def list_check_runs(self, repo: str, ref: str) -> list[dict]:
        return list(self._check_runs.get((repo, ref), []))

    def set_check_runs(self, repo: str, ref: str, runs: list[dict]) -> None:
        """Test-only — populates the check-runs "reported by CI"."""
        self._check_runs[(repo, ref)] = runs

    # Phase 3 (WSE-E4-T9b) — targeted re-runs
    rerequest_supported: bool = True
    rerequested: list[tuple[str, int]] = field(default_factory=list)

    def rerequest_check_run(self, repo: str, check_run_id: int) -> bool:
        if not self.rerequest_supported:
            return False
        self.rerequested.append((repo, check_run_id))
        # mimics GitHub: a re-run check-run goes back to "queued"
        for runs in self._check_runs.values():
            for run in runs:
                if run.get("id") == check_run_id:
                    run["status"] = "queued"
                    run["conclusion"] = None
        return True

    # Phase 4 (WSE-E6-T16) — review threads anchored to commits.
    _review_threads: dict[tuple[str, int], list[dict]] = field(default_factory=dict)

    def set_review_threads(self, repo: str, pr_number: int, threads: list[dict]) -> None:
        """Test-only — populates the review threads "anchored to commits"."""
        self._review_threads[(repo, pr_number)] = threads

    def list_review_threads(self, repo: str, pr_number: int) -> list[dict]:
        return list(self._review_threads.get((repo, pr_number), []))

    def authenticated_remote_url(self, repo: str) -> str:
        return f"https://x-access-token:fake-local-token@github.com/{repo}.git"


def build_github_client(cfg: GitHubConfig | None = None) -> GitHubClient:
    cfg = cfg or GitHubConfig()
    if cfg.is_configured:
        logger.info("dse_validation: using RealGitHubClient (GitHub App configured)")
        return RealGitHubClient(cfg)
    logger.warning(
        "dse_validation: GITHUB_APP_ID/GITHUB_APP_PRIVATE_KEY/GITHUB_APP_INSTALLATION_ID "
        "not configured — using FakeGitHubClient (local/test mode, NOT production)"
    )
    return FakeGitHubClient()
