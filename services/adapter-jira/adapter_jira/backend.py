"""WSA-E5-T3 — Jira client (real transport via `requests` + in-memory
`FakeJiraClient` fixture) and `JiraCommentBackend` (implements
`dse_contracts.mutable_comment.CommentBackend`, the third backend of the same
`MutableCommentWriter` already used by Slack and GitHub).

Authentication: Basic auth with the service account email + a scoped
(project-level) API token, read from Vault (`adapter_jira.config`). With no
real Jira site in this session, `FakeJiraClient` replaces the transport in the
tests — the `JiraCommentBackend`/transition-serialization/poller logic is 100%
real.

REST API v3 (the current one for Jira Cloud). Comment body in ADF (Atlassian
Document Format) — `_adf()` wraps plain text in the minimal accepted doc.
"""
from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

import requests

logger = logging.getLogger("adapter_jira.backend")


def _adf(text: str) -> dict[str, Any]:
    """Minimal ADF doc (a single paragraph) — the format required by the v3 API
    comment endpoint. `FakeJiraClient` ignores the format and stores the text
    exactly as it came in."""
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


class JiraClientLike(Protocol):
    def add_comment(self, key: str, body: str) -> str: ...
    def update_comment(self, key: str, comment_id: str, body: str) -> None: ...
    def get_transitions(self, key: str) -> list[dict[str, Any]]: ...
    def transition_issue(self, key: str, transition_id: str) -> None: ...
    def search_updated(self, project_key: str, since_iso: str | None) -> list[dict[str, Any]]: ...
    def get_comments(self, key: str) -> list[dict[str, Any]]: ...


class JiraCommentBackend:
    """`CommentBackend` for the `MutableCommentWriter` (surface='jira').
    `surface_ref` = `{"ticket_key": "DSE-123"}`; opaque `comment_ref` =
    JSON `{"ticket_key", "comment_id"}`."""

    def __init__(self, client: JiraClientLike):
        self._client = client

    def post(self, surface_ref: dict, body: str) -> str:
        key = surface_ref["ticket_key"]
        comment_id = self._client.add_comment(key, body)
        return json.dumps({"ticket_key": key, "comment_id": comment_id})

    def edit(self, surface_ref: dict, comment_ref: str, body: str) -> None:
        ref = json.loads(comment_ref)
        self._client.update_comment(ref["ticket_key"], ref["comment_id"], body)


class RealJiraClient:
    """Real transport against the Jira Cloud REST API v3."""

    def __init__(self, base_url: str, email: str, api_token: str, *, session: requests.Session | None = None):
        self._base = base_url.rstrip("/")
        self._auth = base64.b64encode(f"{email}:{api_token}".encode()).decode()
        self._http = session or requests.Session()
        self._account_id: str | None = None

    def self_account_id(self) -> str | None:
        """accountId of the account the DSE posts as, cached after the first call.

        Needed to recognise the DSE's OWN comments: Jira attributes them to the
        token owner, so without this the poller reads the bot's question back as
        the human's answer (see `ingest.ingest_comment`). Best-effort — if the
        lookup fails, returning None only means the filter is inert, never that
        the flow breaks.
        """
        if self._account_id is None:
            try:
                resp = self._http.get(f"{self._base}/rest/api/3/myself", headers=self._headers(), timeout=10)
                resp.raise_for_status()
                self._account_id = resp.json().get("accountId")
            except Exception:  # noqa: BLE001 — identity lookup must never break ingestion
                logger.warning("could not resolve the DSE's own Jira accountId", exc_info=True)
        return self._account_id

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Basic {self._auth}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def add_comment(self, key: str, body: str) -> str:
        resp = self._http.post(
            f"{self._base}/rest/api/3/issue/{key}/comment",
            headers=self._headers(),
            json={"body": _adf(body)},
            timeout=10,
        )
        resp.raise_for_status()
        return str(resp.json()["id"])

    def update_comment(self, key: str, comment_id: str, body: str) -> None:
        resp = self._http.put(
            f"{self._base}/rest/api/3/issue/{key}/comment/{comment_id}",
            headers=self._headers(),
            json={"body": _adf(body)},
            timeout=10,
        )
        resp.raise_for_status()

    def get_transitions(self, key: str) -> list[dict[str, Any]]:
        resp = self._http.get(
            f"{self._base}/rest/api/3/issue/{key}/transitions",
            headers=self._headers(),
            timeout=10,
        )
        resp.raise_for_status()
        out = []
        for t in resp.json().get("transitions", []):
            out.append({"id": str(t["id"]), "name": t.get("name", ""), "to_status": (t.get("to") or {}).get("name", "")})
        return out

    def transition_issue(self, key: str, transition_id: str) -> None:
        resp = self._http.post(
            f"{self._base}/rest/api/3/issue/{key}/transitions",
            headers=self._headers(),
            json={"transition": {"id": transition_id}},
            timeout=10,
        )
        resp.raise_for_status()

    def search_updated(self, project_key: str, since_iso: str | None) -> list[dict[str, Any]]:
        jql = f'project = "{project_key}"'
        if since_iso:
            # The caller passes a RELATIVE bound (`-90m`), not a timestamp. An
            # absolute JQL literal is read in the Jira account's timezone, so a
            # UTC cursor queried into the future and matched nothing — see
            # `poller._relative_bound`. Relative bounds have no timezone.
            jql += f' AND updated >= "{since_iso}"'
        jql += " ORDER BY updated ASC"
        # New /search/jql endpoint: the old /rest/api/3/search was REMOVED by
        # Atlassian (410 Gone, 2025). Pagination by nextPageToken (no longer
        # startAt/total); iterates until isLast so no issues beyond the first
        # page are lost.
        # `reporter` is MANDATORY (BD-39 finding, 2026-07-23): without it the
        # poller creates the WorkItem with requester=system:adapter-jira-poller,
        # and then a clarification answer from the ticket's OWN author does not
        # authorize (the author's principal != the system principal) — the flow
        # gets stuck in needs_clarification forever.
        fields = ["summary", "description", "labels", "status", "project", "reporter"]
        issues: list[dict[str, Any]] = []
        next_token: str | None = None
        while True:
            body: dict[str, Any] = {"jql": jql, "fields": fields, "maxResults": 100}
            if next_token:
                body["nextPageToken"] = next_token
            resp = self._http.post(
                f"{self._base}/rest/api/3/search/jql",
                headers=self._headers(),
                json=body,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            issues.extend(data.get("issues", []))
            next_token = data.get("nextPageToken")
            if data.get("isLast") or not next_token:
                break
        return issues

    def get_comments(self, key: str) -> list[dict[str, Any]]:
        resp = self._http.get(
            f"{self._base}/rest/api/3/issue/{key}/comment",
            headers=self._headers(),
            timeout=10,
        )
        resp.raise_for_status()
        out = []
        for c in resp.json().get("comments", []):
            # `renderedBody`/`body` ADF -> text: best effort for the poller.
            body = c.get("body")
            text = body if isinstance(body, str) else _flatten_adf(body)
            out.append({"id": str(c["id"]), "body": text, "author": c.get("author") or {}})
        return out


def _flatten_adf(node: Any) -> str:
    """Extracts text from an ADF doc (best-effort) so the poller can rebuild
    the same content_snapshot as the webhook produced when it carried plain
    text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        return "".join(_flatten_adf(ch) for ch in node.get("content", []))
    if isinstance(node, list):
        return "".join(_flatten_adf(ch) for ch in node)
    return ""


@dataclass
class FakeJiraClient:
    """In-memory fixture (documented — this is not the real API). Simulates the
    available transitions, comments and search. Used in the tests in place of
    the HTTP transport; the logic around it (backend/serialization/poller) is
    the real one."""

    # ticket_key -> list of {id, name, to_status}
    transitions_by_ticket: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # ticket_key -> current status (updated by transition_issue)
    status_by_ticket: dict[str, str] = field(default_factory=dict)
    # ticket_key -> {comment_id: body}
    comments: dict[str, dict[str, str]] = field(default_factory=dict)
    # issues indexable by the poller's search: project_key -> list[issue]
    issues_by_project: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    transition_calls: list[dict[str, Any]] = field(default_factory=list)
    # accountId the fixture claims to post as; tests set it to exercise the
    # self-authored comment filter.
    account_id: str | None = None
    _next_comment_id: int = 9000

    def self_account_id(self) -> str | None:
        return self.account_id

    def add_comment(self, key: str, body: str) -> str:
        self._next_comment_id += 1
        cid = str(self._next_comment_id)
        self.comments.setdefault(key, {})[cid] = body
        return cid

    def update_comment(self, key: str, comment_id: str, body: str) -> None:
        if comment_id not in self.comments.get(key, {}):
            raise KeyError(f"update_comment on nonexistent comment_id: {key}/{comment_id}")
        self.comments[key][comment_id] = body

    def get_transitions(self, key: str) -> list[dict[str, Any]]:
        return list(self.transitions_by_ticket.get(key, []))

    def transition_issue(self, key: str, transition_id: str) -> None:
        avail = self.transitions_by_ticket.get(key, [])
        match = next((t for t in avail if str(t["id"]) == str(transition_id)), None)
        if match is None:
            raise ValueError(f"nonexistent transition {transition_id} for {key}")
        self.status_by_ticket[key] = match["to_status"]
        self.transition_calls.append({"key": key, "transition_id": transition_id, "to_status": match["to_status"]})

    def search_updated(self, project_key: str, since_iso: str | None) -> list[dict[str, Any]]:
        return list(self.issues_by_project.get(project_key, []))

    def get_comments(self, key: str) -> list[dict[str, Any]]:
        return [{"id": cid, "body": body, "author": {}} for cid, body in self.comments.get(key, {}).items()]


def build_real_jira_client() -> RealJiraClient:
    from . import config

    return RealJiraClient(config.get_base_url(), config.get_service_account_email(), config.get_api_token())
