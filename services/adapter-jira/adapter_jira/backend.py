"""WSA-E5-T3 — cliente Jira (transporte real via `requests` + fixture
in-memory `FakeJiraClient`) e `JiraCommentBackend` (implementa
`dse_contracts.mutable_comment.CommentBackend`, terceiro backend da mesma
`MutableCommentWriter` já usada por Slack e GitHub).

Autenticação: Basic auth com email da service account + API token escopado
(project-level), lidos do Vault (`adapter_jira.config`). Sem site Jira real
nesta sessão, `FakeJiraClient` substitui o transporte nos testes — a lógica de
`JiraCommentBackend`/serialização de transição/poller é 100% real.

REST API v3 (atual do Jira Cloud). Corpo de comentário em ADF (Atlassian
Document Format) — `_adf()` embrulha o texto simples no doc mínimo aceito.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

import requests


def _adf(text: str) -> dict[str, Any]:
    """Doc ADF mínimo (um parágrafo) — formato exigido pelo endpoint de
    comentário da API v3. O `FakeJiraClient` ignora o formato e guarda o texto
    como veio."""
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
    """`CommentBackend` para a `MutableCommentWriter` (surface='jira').
    `surface_ref` = `{"ticket_key": "DSE-123"}`; `comment_ref` opaco =
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
    """Transporte real contra a REST API v3 do Jira Cloud."""

    def __init__(self, base_url: str, email: str, api_token: str, *, session: requests.Session | None = None):
        self._base = base_url.rstrip("/")
        self._auth = base64.b64encode(f"{email}:{api_token}".encode()).decode()
        self._http = session or requests.Session()

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
            # Jira JQL usa 'updated >= "yyyy-MM-dd HH:mm"' — o caller formata.
            jql += f' AND updated >= "{since_iso}"'
        jql += " ORDER BY updated ASC"
        # Endpoint novo /search/jql: o antigo /rest/api/3/search foi REMOVIDO
        # pela Atlassian (410 Gone, 2025). Paginação por nextPageToken (não mais
        # startAt/total); itera até isLast para não perder issues além da página.
        # `reporter` é OBRIGATÓRIO (achado BD-39, 2026-07-23): sem ele o poller
        # cria o WorkItem com requester=system:adapter-jira-poller, e aí a
        # resposta de clarificação do PRÓPRIO autor do ticket não autoriza
        # (principal do autor != system principal) — o fluxo trava em
        # needs_clarification para sempre.
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
            # `renderedBody`/`body` ADF -> texto: melhor esforço para o poller.
            body = c.get("body")
            text = body if isinstance(body, str) else _flatten_adf(body)
            out.append({"id": str(c["id"]), "body": text, "author": c.get("author") or {}})
        return out


def _flatten_adf(node: Any) -> str:
    """Extrai texto de um doc ADF (best-effort) para o poller reconstruir o
    content_snapshot igual ao do webhook quando este trouxe texto simples."""
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
    """Fixture in-memory (documentado — não é a API real). Simula transições
    disponíveis, comentários e busca. Usado nos testes no lugar do transporte
    HTTP; a lógica ao redor (backend/serialização/poller) é a real."""

    # ticket_key -> lista de {id, name, to_status}
    transitions_by_ticket: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # ticket_key -> status atual (atualizado por transition_issue)
    status_by_ticket: dict[str, str] = field(default_factory=dict)
    # ticket_key -> {comment_id: body}
    comments: dict[str, dict[str, str]] = field(default_factory=dict)
    # issues indexáveis por busca do poller: project_key -> list[issue]
    issues_by_project: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    transition_calls: list[dict[str, Any]] = field(default_factory=list)
    _next_comment_id: int = 9000

    def add_comment(self, key: str, body: str) -> str:
        self._next_comment_id += 1
        cid = str(self._next_comment_id)
        self.comments.setdefault(key, {})[cid] = body
        return cid

    def update_comment(self, key: str, comment_id: str, body: str) -> None:
        if comment_id not in self.comments.get(key, {}):
            raise KeyError(f"update_comment em comment_id inexistente: {key}/{comment_id}")
        self.comments[key][comment_id] = body

    def get_transitions(self, key: str) -> list[dict[str, Any]]:
        return list(self.transitions_by_ticket.get(key, []))

    def transition_issue(self, key: str, transition_id: str) -> None:
        avail = self.transitions_by_ticket.get(key, [])
        match = next((t for t in avail if str(t["id"]) == str(transition_id)), None)
        if match is None:
            raise ValueError(f"transição inexistente {transition_id} para {key}")
        self.status_by_ticket[key] = match["to_status"]
        self.transition_calls.append({"key": key, "transition_id": transition_id, "to_status": match["to_status"]})

    def search_updated(self, project_key: str, since_iso: str | None) -> list[dict[str, Any]]:
        return list(self.issues_by_project.get(project_key, []))

    def get_comments(self, key: str) -> list[dict[str, Any]]:
        return [{"id": cid, "body": body, "author": {}} for cid, body in self.comments.get(key, {}).items()]


def build_real_jira_client() -> RealJiraClient:
    from . import config

    return RealJiraClient(config.get_base_url(), config.get_service_account_email(), config.get_api_token())
