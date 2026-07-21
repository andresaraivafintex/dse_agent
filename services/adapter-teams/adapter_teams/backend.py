"""Outbound Teams — `CommentBackend` (dse_contracts.mutable_comment): quarto
backend da MESMA `MutableCommentWriter` já usada por Slack/GitHub/Jira — logo
"exatamente 1 mensagem de status por WorkItem, editada in-place" vale de graça
para Teams (a lógica comum vive no writer compartilhado).

Transporte real: Bot Framework Connector REST (o mesmo que a Azure Bot Service
usa), autenticado por bearer token de client_credentials do AAD:
  - post   -> POST {serviceUrl}/v3/conversations/{conversationId}/activities
              (cria a mensagem de status; devolve o `activity id`)
  - edit   -> PUT  {serviceUrl}/v3/conversations/{conversationId}/activities/{activityId}
              (edita a mensagem in-place)

PROVISÃO: sem app registration / tenant Teams real nesta sessão, `FakeTeamsClient`
substitui o transporte nos testes — a lógica de `TeamsCommentBackend` (serialização
do comment_ref, escolha post-vs-edit via o writer) é 100% real. `build_real_teams_client`
constrói o cliente HTTP real (via `requests`), exercitado em produção após ativação.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Protocol


class TeamsClientLike(Protocol):
    def send_activity(self, *, service_url: str, conversation_id: str, text: str) -> str:
        """Cria uma activity de mensagem na conversa. Retorna o `activity id`."""
        ...

    def update_activity(
        self, *, service_url: str, conversation_id: str, activity_id: str, text: str
    ) -> None:
        """Edita in-place a activity existente."""
        ...


class TeamsCommentBackend:
    """Implementa `dse_contracts.mutable_comment.CommentBackend`.

    `surface_ref` esperado: `{"conversation_id": ..., "service_url": ...}`.
    O `comment_ref` opaco serializa o suficiente para uma edição futura
    (conversation_id + activity_id + service_url), igual aos outros backends.
    """

    def __init__(self, client: TeamsClientLike):
        self._client = client

    def post(self, surface_ref: dict, body: str) -> str:
        service_url = surface_ref["service_url"]
        conversation_id = surface_ref["conversation_id"]
        activity_id = self._client.send_activity(
            service_url=service_url, conversation_id=conversation_id, text=body
        )
        return json.dumps(
            {
                "service_url": service_url,
                "conversation_id": conversation_id,
                "activity_id": activity_id,
            }
        )

    def edit(self, surface_ref: dict, comment_ref: str, body: str) -> None:
        ref = json.loads(comment_ref)
        self._client.update_activity(
            service_url=ref["service_url"],
            conversation_id=ref["conversation_id"],
            activity_id=ref["activity_id"],
            text=body,
        )


@dataclass
class FakeTeamsClient:
    """Fixture in-memory usada nos testes (documentado — NÃO é a API real).
    Registra cada send/update para os testes afirmarem 'exatamente 1 send +
    N updates, nunca N sends' (a invariante de 1-mensagem-por-tarefa)."""

    _next_id: int = 1000
    activities: dict[str, str] = field(default_factory=dict)  # activity_id -> texto atual
    send_calls: list[dict] = field(default_factory=list)
    update_calls: list[dict] = field(default_factory=list)

    def send_activity(self, *, service_url: str, conversation_id: str, text: str) -> str:
        self._next_id += 1
        activity_id = f"1{self._next_id}"
        self.activities[activity_id] = text
        self.send_calls.append(
            {"service_url": service_url, "conversation_id": conversation_id, "text": text, "activity_id": activity_id}
        )
        return activity_id

    def update_activity(
        self, *, service_url: str, conversation_id: str, activity_id: str, text: str
    ) -> None:
        if activity_id not in self.activities:
            raise KeyError(f"update_activity em activity inexistente: {activity_id}")
        self.activities[activity_id] = text
        self.update_calls.append(
            {"service_url": service_url, "conversation_id": conversation_id, "activity_id": activity_id, "text": text}
        )


class RealTeamsClient:
    """Cliente real do Bot Framework Connector (transporte `requests`). Obtém e
    cacheia o bearer token de client_credentials do AAD e faz POST/PUT de
    activities. Exercitado em produção após ativação; nos testes é substituído
    pelo `FakeTeamsClient`."""

    _TOKEN_URL = "https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token"
    _SCOPE = "https://api.botframework.com/.default"

    def __init__(self, app_id: str, app_password: str):
        self._app_id = app_id
        self._app_password = app_password
        self._token: str | None = None
        self._token_exp: float = 0.0

    def _bearer(self) -> str:
        import requests

        now = time.time()
        if self._token and now < self._token_exp - 60:
            return self._token
        resp = requests.post(
            self._TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self._app_id,
                "client_secret": self._app_password,
                "scope": self._SCOPE,
            },
            timeout=15,
        )
        resp.raise_for_status()
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_exp = now + int(payload.get("expires_in", 3600))
        return self._token

    def send_activity(self, *, service_url: str, conversation_id: str, text: str) -> str:
        import requests

        url = f"{service_url.rstrip('/')}/v3/conversations/{conversation_id}/activities"
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {self._bearer()}"},
            json={"type": "message", "text": text},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()["id"]

    def update_activity(
        self, *, service_url: str, conversation_id: str, activity_id: str, text: str
    ) -> None:
        import requests

        url = f"{service_url.rstrip('/')}/v3/conversations/{conversation_id}/activities/{activity_id}"
        resp = requests.put(
            url,
            headers={"Authorization": f"Bearer {self._bearer()}"},
            json={"type": "message", "text": text},
            timeout=15,
        )
        resp.raise_for_status()


def build_real_teams_client(app_id: str, app_password: str) -> RealTeamsClient:
    return RealTeamsClient(app_id, app_password)
