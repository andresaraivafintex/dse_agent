"""WSA-E4-T2 — outbound: `CommentBackend` real contra a API do GitHub
(`POST/PATCH /repos/{repo}/issues/{issue}/comments`), sob identidade GitHub
App (`RealGithubClient` usa um installation access token de
`adapter_github.auth.get_installation_access_token`, nunca um PAT pessoal).

`FakeGithubClient` é o fixture in-memory usado nos testes — mesma
convenção do `FakeSlackClient` do adapter-slack: a lógica de
`GithubCommentBackend`/`MutableCommentWriter` é 100% real, só o transporte
HTTP é substituído.
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


class GithubCommentBackend:
    """Implementa `dse_contracts.mutable_comment.CommentBackend`."""

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
    """Cliente HTTP real (sem PyGithub — `requests` puro é suficiente e
    mantém a dependência mínima, P7 boring-first) autenticado com um
    installation access token de GitHub App."""

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


@dataclass
class FakeGithubClient:
    """In-memory fixture (documentado — não é a API real)."""

    _next_id: int = 1000
    comments: dict[int, str] = field(default_factory=dict)  # comment_id -> body atual
    create_calls: list[dict] = field(default_factory=list)
    update_calls: list[dict] = field(default_factory=list)

    def create_comment(self, repo: str, issue_number: int, body: str) -> int:
        self._next_id += 1
        comment_id = self._next_id
        self.comments[comment_id] = body
        self.create_calls.append({"repo": repo, "issue_number": issue_number, "body": body, "comment_id": comment_id})
        return comment_id

    def update_comment(self, repo: str, comment_id: int, body: str) -> None:
        if comment_id not in self.comments:
            raise KeyError(f"update_comment em comment_id inexistente: {comment_id}")
        self.comments[comment_id] = body
        self.update_calls.append({"repo": repo, "comment_id": comment_id, "body": body})


def build_real_github_client() -> RealGithubClient:
    """Constrói o `RealGithubClient` autenticado via GitHub App (nunca PAT
    pessoal). Import feito aqui dentro para isolar a dependência de rede da
    autenticação App do resto do módulo."""
    from . import config
    from .auth import get_installation_access_token

    token = get_installation_access_token(
        app_id=config.get_app_id(),
        private_key_pem=config.get_app_private_key(),
        installation_id=config.get_installation_id(),
    )
    return RealGithubClient(token)
