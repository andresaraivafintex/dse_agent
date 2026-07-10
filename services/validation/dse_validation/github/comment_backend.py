"""WSE-E3-T7 — backend GitHub para `dse_contracts.mutable_comment.CommentBackend`,
reutilizado pelo `MutableCommentWriter` já pronto na fundação (não reimplementa
o comentário mutável, só o transporte GitHub). Se `services/adapter-github`
(WS-A) já tiver publicado um backend equivalente para comentários de issue
quando este código for integrado, prefira aquele — ver README §Cross-workstream
para a decisão de qual módulo é fonte de verdade.

`surface_ref` esperado: `{"repo": "org/name", "issue_number": 42}` (PRs no
GitHub usam a mesma API de comentário de issue que issues comuns)."""
from __future__ import annotations

from dse_validation.github.client import GitHubClient


class GitHubCommentBackend:
    def __init__(self, client: GitHubClient):
        self._client = client

    def post(self, surface_ref: dict, body: str) -> str:
        return self._client.post_issue_comment(surface_ref["repo"], surface_ref["issue_number"], body)

    def edit(self, surface_ref: dict, comment_ref: str, body: str) -> None:
        self._client.edit_issue_comment(surface_ref["repo"], comment_ref, body)
