"""WSE-E3-T7 — GitHub backend for `dse_contracts.mutable_comment.CommentBackend`,
reused by the `MutableCommentWriter` already available in the foundation (does
not reimplement the mutable comment, only the GitHub transport). If
`services/adapter-github` (WS-A) has already published an equivalent backend for
issue comments by the time this code is integrated, prefer that one — see README
§Cross-workstream for the decision on which module is the source of truth.

Expected `surface_ref`: `{"repo": "org/name", "issue_number": 42}` (PRs on
GitHub use the same issue-comment API as regular issues)."""
from __future__ import annotations

from dse_validation.github.client import GitHubClient


class GitHubCommentBackend:
    def __init__(self, client: GitHubClient):
        self._client = client

    def post(self, surface_ref: dict, body: str) -> str:
        return self._client.post_issue_comment(surface_ref["repo"], surface_ref["issue_number"], body)

    def edit(self, surface_ref: dict, comment_ref: str, body: str) -> None:
        self._client.edit_issue_comment(surface_ref["repo"], comment_ref, body)
