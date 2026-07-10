"""WSA-E3-T2 — outbound: `CommentBackend` real contra a Slack Web API
(`chat.postMessage`/`chat.update`), usado pelo
`dse_contracts.mutable_comment.MutableCommentWriter` compartilhado —
exatamente 1 mensagem de status por tarefa, editada in-place.

Sem credencial real de Slack App nesta sessão: a LÓGICA é real (usa
`slack_sdk.WebClient`, os mesmos métodos que rodariam contra a API de
verdade), só o token (`SLACK_BOT_TOKEN`) é fixture/local. `FakeSlackClient`
abaixo implementa a mesma superfície (`chat_postMessage`/`chat_update`) e é
injetado no lugar de `slack_sdk.WebClient` nos testes — `SlackCommentBackend`
não sabe (nem precisa saber) qual dos dois recebeu.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol


class _SlackClientLike(Protocol):
    def chat_postMessage(self, *, channel: str, text: str) -> dict: ...
    def chat_update(self, *, channel: str, ts: str, text: str) -> dict: ...


class SlackCommentBackend:
    """Implementa `dse_contracts.mutable_comment.CommentBackend`."""

    def __init__(self, client: _SlackClientLike):
        self._client = client

    def post(self, surface_ref: dict, body: str) -> str:
        channel = surface_ref["channel"]
        resp = self._client.chat_postMessage(channel=channel, text=body)
        ts = resp["ts"]
        return json.dumps({"channel": channel, "ts": ts})

    def edit(self, surface_ref: dict, comment_ref: str, body: str) -> None:
        ref = json.loads(comment_ref)
        self._client.chat_update(channel=ref["channel"], ts=ref["ts"], text=body)


@dataclass
class FakeSlackClient:
    """In-memory fixture usada nos testes (documentado — não é a API real).
    Registra cada post/update para os testes poderem afirmar
    'exatamente 1 post + N updates, nunca N posts'."""

    _next_ts: float = 1000.0
    messages: dict[str, str] = field(default_factory=dict)  # ts -> text (estado atual)
    post_calls: list[dict] = field(default_factory=list)
    update_calls: list[dict] = field(default_factory=list)

    def chat_postMessage(self, *, channel: str, text: str) -> dict:
        self._next_ts += 1
        ts = f"{self._next_ts:.6f}"
        self.messages[ts] = text
        self.post_calls.append({"channel": channel, "text": text, "ts": ts})
        return {"ok": True, "channel": channel, "ts": ts}

    def chat_update(self, *, channel: str, ts: str, text: str) -> dict:
        if ts not in self.messages:
            raise KeyError(f"chat_update em ts inexistente: {ts}")
        self.messages[ts] = text
        self.update_calls.append({"channel": channel, "text": text, "ts": ts})
        return {"ok": True, "channel": channel, "ts": ts}


def build_real_slack_client(bot_token: str):
    """Constrói o `slack_sdk.WebClient` real. Import feito aqui dentro (não
    no topo do módulo) para o `FakeSlackClient`/testes não exigirem
    `slack_sdk` instalado em ambientes que só rodam testes offline —
    embora, na prática, `slack_sdk` é dependência declarada no pyproject."""
    from slack_sdk import WebClient

    return WebClient(token=bot_token)
