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
    def chat_postMessage(self, *, channel: str, text: str, blocks: list | None = ...) -> dict: ...
    def chat_update(self, *, channel: str, ts: str, text: str, blocks: list | None = ...) -> dict: ...


def approval_blocks(body: str) -> list[dict]:
    """Block Kit da aprovação de plano (Fase B / relatório 07): o texto do
    status + botões Approve/Reject. Os `action_id`/`value` são os marcadores
    que `parse_slack_approval` lê (verdict/route determinístico — C1). Sem
    postar estes botões, o humano não tinha como aprovar/rejeitar pelo Slack."""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
        {
            "type": "actions",
            "block_id": "dse_plan_approval",
            "elements": [
                {"type": "button", "action_id": "dse_plan_approve", "style": "primary",
                 "text": {"type": "plain_text", "text": "Approve"}, "value": "approve"},
                {"type": "button", "action_id": "dse_plan_reject", "style": "danger",
                 "text": {"type": "plain_text", "text": "Reject"}, "value": "reject:re_plan"},
            ],
        },
    ]


class SlackCommentBackend:
    """Implementa `dse_contracts.mutable_comment.CommentBackend`. `surface_ref`
    pode carregar `blocks` (Slack-specific) — quando presente, a mensagem é
    postada/editada com Block Kit (ex.: botões de aprovação); senão, texto
    puro. O contrato compartilhado (body: str) fica intacto."""

    def __init__(self, client: _SlackClientLike):
        self._client = client

    def post(self, surface_ref: dict, body: str) -> str:
        channel = surface_ref["channel"]
        blocks = surface_ref.get("blocks")
        kwargs = {"channel": channel, "text": body}
        if blocks:
            kwargs["blocks"] = blocks
        resp = self._client.chat_postMessage(**kwargs)
        ts = resp["ts"]
        return json.dumps({"channel": channel, "ts": ts})

    def edit(self, surface_ref: dict, comment_ref: str, body: str) -> None:
        ref = json.loads(comment_ref)
        blocks = surface_ref.get("blocks")
        kwargs = {"channel": ref["channel"], "ts": ref["ts"], "text": body}
        if blocks:
            kwargs["blocks"] = blocks
        self._client.chat_update(**kwargs)


@dataclass
class FakeSlackClient:
    """In-memory fixture usada nos testes (documentado — não é a API real).
    Registra cada post/update para os testes poderem afirmar
    'exatamente 1 post + N updates, nunca N posts'."""

    _next_ts: float = 1000.0
    messages: dict[str, str] = field(default_factory=dict)  # ts -> text (estado atual)
    post_calls: list[dict] = field(default_factory=list)
    update_calls: list[dict] = field(default_factory=list)

    def chat_postMessage(self, *, channel: str, text: str, blocks: list | None = None) -> dict:
        self._next_ts += 1
        ts = f"{self._next_ts:.6f}"
        self.messages[ts] = text
        self.post_calls.append({"channel": channel, "text": text, "ts": ts, "blocks": blocks})
        return {"ok": True, "channel": channel, "ts": ts}

    def chat_update(self, *, channel: str, ts: str, text: str, blocks: list | None = None) -> dict:
        if ts not in self.messages:
            raise KeyError(f"chat_update em ts inexistente: {ts}")
        self.messages[ts] = text
        self.update_calls.append({"channel": channel, "text": text, "ts": ts, "blocks": blocks})
        return {"ok": True, "channel": channel, "ts": ts}


def build_real_slack_client(bot_token: str):
    """Constrói o `slack_sdk.WebClient` real. Import feito aqui dentro (não
    no topo do módulo) para o `FakeSlackClient`/testes não exigirem
    `slack_sdk` instalado em ambientes que só rodam testes offline —
    embora, na prática, `slack_sdk` é dependência declarada no pyproject."""
    from slack_sdk import WebClient

    return WebClient(token=bot_token)
