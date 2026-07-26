"""WSA-E4-T2 — outbound: real `CommentBackend` against the GitHub API
(`POST/PATCH /repos/{repo}/issues/{issue}/comments`), under the GitHub App
identity (`RealGithubClient` uses an installation access token from
`adapter_github.auth.get_installation_access_token`, never a personal PAT).

`FakeGithubClient` is the in-memory fixture used in the tests — same
convention as adapter-slack's `FakeSlackClient`: the
`GithubCommentBackend`/`MutableCommentWriter` logic is 100% real, only the
HTTP transport is replaced.

The client also READS a thread back (`get_issue`, `list_issue_comments`) for the
reply reconciler (`app.py`, `/internal/reconcile`) — the one place in this
adapter that is allowed to re-read a message, and only for clarification
replies, never for an approval (the TOCTOU rule of WSA-E2-T2).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

import requests

GITHUB_API_BASE = "https://api.github.com"


class _GithubClientLike(Protocol):
    def create_comment(self, repo: str, issue_number: int, body: str) -> int: ...
    def update_comment(self, repo: str, comment_id: int, body: str) -> None: ...


class GithubReaderLike(Protocol):
    """Read side, used by the reply reconciler (`/internal/reconcile`).

    A protocol of its own rather than more methods on `_GithubClientLike`: the
    outbound writer needs nothing but create/update, and widening its contract
    would force every comment backend fixture to implement a read path it never
    uses.
    """

    def get_issue(self, repo: str, number: int) -> dict: ...
    def list_issue_comments(self, repo: str, number: int) -> list[dict]: ...


class GithubCommentBackend:
    """Implements `dse_contracts.mutable_comment.CommentBackend`."""

    def __init__(self, client: _GithubClientLike):
        self._client = client

    def post(self, surface_ref: dict, body: str) -> str:
        repo = surface_ref["repo"]
        issue_number = surface_ref["number"]
        comment_id = self._client.create_comment(repo, issue_number, body)
        return json.dumps({"repo": repo, "comment_id": comment_id})

    def edit(self, surface_ref: dict, comment_ref: str, body: str) -> None:
        ref = json.loads(comment_ref)
        self._client.update_comment(ref["repo"], ref["comment_id"], body)


class RealGithubClient:
    """Real HTTP client (no PyGithub — plain `requests` is enough and keeps the
    dependency surface minimal, P7 boring-first) authenticated with a GitHub App
    installation access token."""

    def __init__(self, installation_token: str):
        self._token = installation_token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def create_comment(self, repo: str, issue_number: int, body: str) -> int:
        resp = requests.post(
            f"{GITHUB_API_BASE}/repos/{repo}/issues/{issue_number}/comments",
            headers=self._headers(),
            json={"body": body},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["id"]

    def update_comment(self, repo: str, comment_id: int, body: str) -> None:
        resp = requests.patch(
            f"{GITHUB_API_BASE}/repos/{repo}/issues/comments/{comment_id}",
            headers=self._headers(),
            json={"body": body},
            timeout=10,
        )
        resp.raise_for_status()

    def get_issue(self, repo: str, number: int) -> dict:
        """The issue (or PR — GitHub shares the number namespace) as the API
        returns it. The reconciler needs the whole object, not just the body:
        `pull_request` tells a PR apart from a plain issue, and `labels` feeds
        the same task classification the webhook payload provides."""
        resp = requests.get(
            f"{GITHUB_API_BASE}/repos/{repo}/issues/{number}",
            headers=self._headers(),
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def list_issue_comments(self, repo: str, number: int, *, max_pages: int = 5) -> list[dict]:
        """Conversation comments of an issue/PR — the same objects the
        `issue_comment` webhook delivers one at a time (review comments live on
        another endpoint and are NOT returned here, matching the webhook split).

        `max_pages` bounds the read: a thread long enough to need a sixth page
        of 100 comments is not a thread whose answer we are still hunting for,
        and an unbounded loop here would let one pathological issue burn the
        installation's whole rate limit.
        """
        collected: list[dict] = []
        per_page = 100
        for page in range(1, max_pages + 1):
            resp = requests.get(
                f"{GITHUB_API_BASE}/repos/{repo}/issues/{number}/comments",
                headers=self._headers(),
                params={"per_page": per_page, "page": page},
                timeout=10,
            )
            resp.raise_for_status()
            batch = resp.json()
            collected.extend(batch)
            if len(batch) < per_page:
                break
        return collected


@dataclass
class FakeGithubClient:
    """In-memory fixture (documented — this is not the real API)."""

    _next_id: int = 1000
    comments: dict[int, str] = field(default_factory=dict)  # comment_id -> current body
    create_calls: list[dict] = field(default_factory=list)
    update_calls: list[dict] = field(default_factory=list)
    # Read side: "<repo>#<number>" -> issue object / list of comment objects,
    # shaped like the real API responses (that is what the reconciler parses).
    issues: dict[str, dict] = field(default_factory=dict)
    issue_comments: dict[str, list[dict]] = field(default_factory=dict)
    get_issue_calls: list[dict] = field(default_factory=list)
    list_comments_calls: list[dict] = field(default_factory=list)

    def create_comment(self, repo: str, issue_number: int, body: str) -> int:
        self._next_id += 1
        comment_id = self._next_id
        self.comments[comment_id] = body
        self.create_calls.append({"repo": repo, "issue_number": issue_number, "body": body, "comment_id": comment_id})
        return comment_id

    def update_comment(self, repo: str, comment_id: int, body: str) -> None:
        if comment_id not in self.comments:
            raise KeyError(f"update_comment on a nonexistent comment_id: {comment_id}")
        self.comments[comment_id] = body
        self.update_calls.append({"repo": repo, "comment_id": comment_id, "body": body})

    @staticmethod
    def _thread_key(repo: str, number: int) -> str:
        return f"{repo}#{number}"

    def seed_issue(self, repo: str, number: int, *, is_pull_request: bool = False,
                   labels: list[str] | None = None, comments: list[dict] | None = None) -> None:
        """Seeds a thread the reconciler can read back."""
        key = self._thread_key(repo, number)
        issue: dict = {"number": number, "title": f"issue {number}", "body": "",
                       "labels": [{"name": name} for name in (labels or [])]}
        if is_pull_request:
            issue["pull_request"] = {"url": f"https://api.github.com/repos/{repo}/pulls/{number}"}
        self.issues[key] = issue
        self.issue_comments[key] = list(comments or [])

    def get_issue(self, repo: str, number: int) -> dict:
        self.get_issue_calls.append({"repo": repo, "number": number})
        key = self._thread_key(repo, number)
        if key not in self.issues:
            # The real API answers 404 here (raise_for_status raises); a fixture
            # that stayed silent would hide a reconciler that reads the wrong
            # thread, and would not exercise the per-item isolation.
            raise KeyError(f"get_issue on a thread that was never seeded: {key}")
        return self.issues[key]

    def list_issue_comments(self, repo: str, number: int) -> list[dict]:
        self.list_comments_calls.append({"repo": repo, "number": number})
        return list(self.issue_comments.get(self._thread_key(repo, number), []))


def build_real_github_client() -> RealGithubClient:
    """Builds the `RealGithubClient` authenticated via GitHub App (never a
    personal PAT). The import is done inside the function to keep the App-auth
    network dependency out of the rest of the module."""
    from . import config
    from .auth import get_installation_access_token

    token = get_installation_access_token(
        app_id=config.get_app_id(),
        private_key_pem=config.get_app_private_key(),
        installation_id=config.get_installation_id(),
    )
    return RealGithubClient(token)
