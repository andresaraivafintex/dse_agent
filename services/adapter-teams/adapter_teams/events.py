"""Normalização Microsoft Teams (Bot Framework Activity) -> `ConversationEvent`.

Espelha `adapter_slack.events` / `adapter_jira.events`. Uma Activity do Teams
(recebida por outgoing webhook ou pelo canal Bot Framework) tem a forma:

    {
      "type": "message",
      "id": "<activity id>",
      "from": {"id": "29:<aad>", "name": "Jane Doe", "aadObjectId": "..."},
      "conversation": {"id": "19:<channel>@thread.tacv2", "conversationType": "channel"},
      "recipient": {"id": "28:<bot>", "name": "DSE Bot"},
      "text": "<at>DSE Bot</at> please fix the login bug",
      "replyToId": "<parent activity id>",           # presente em respostas de thread
      "channelData": {"tenant": {"id": "<aad-tenant-guid>"}},
      "serviceUrl": "https://smba.trafficmanager.net/emea/"
    }

Defesa TOCTOU (WSA-E2-T2): `content_snapshot` vem do `text` do próprio payload
recebido — nunca re-buscamos a mensagem via Graph/connector depois.

Correlação: `source_ref = {"conversation_id": <conversation.id>}` (análogo ao
`{channel, thread_ts}` do Slack). `message_id` é o id da Activity (estado), então
reentregas do mesmo webhook convergem no mesmo `event_id` determinístico
(defesa de idempotência #4).

A construção do `ConversationEvent` tipado passa por `platform_compat.teams_platform()`,
que levanta `TeamsNotActivated` enquanto a fundação não expõe `Platform.teams`
(ver platform_compat.py / README) — os extratores puros abaixo NÃO dependem disso
e são plenamente testáveis já.
"""
from __future__ import annotations

import re
from typing import Any

from dse_contracts import Actor, ConversationEvent, EventKind

from .platform_compat import teams_platform

_AT_TAG = re.compile(r"<at\b[^>]*>.*?</at>", re.IGNORECASE | re.DOTALL)

# String de plataforma para o event_id determinístico. Bate com o VALUE que
# `Platform.teams` terá quando a fundação for estendida (o enum é StrEnum:
# Platform.teams.value == "teams") — logo o event_id computado agora é o mesmo
# que o pipeline tipado produzirá pós-ativação.
PLATFORM_STR = "teams"


def conversation_id(activity: dict[str, Any]) -> str:
    return (activity.get("conversation") or {}).get("id", "")


def activity_id(activity: dict[str, Any]) -> str:
    return activity.get("id", "")


def service_url(activity: dict[str, Any]) -> str:
    return activity.get("serviceUrl", "")


def aad_tenant_id(activity: dict[str, Any]) -> str | None:
    """AAD tenant guid — a `binding_key` de resolução tenant Teams -> tenant DSE
    (WSA-E1-T5) na ativação."""
    tenant = ((activity.get("channelData") or {}).get("tenant") or {})
    return tenant.get("id")


def actor_of(activity: dict[str, Any]) -> tuple[str, str | None]:
    """(platform_user_id, display_name). O id do usuário Teams é o `from.id`
    (`29:<aad>`); preferimos o `aadObjectId` estável quando presente."""
    frm = activity.get("from") or {}
    user_id = frm.get("aadObjectId") or frm.get("id", "")
    return user_id, frm.get("name")


def clean_text(activity: dict[str, Any]) -> str:
    """Remove as tags `<at>...</at>` de menção, deixando o texto do usuário —
    o mesmo que o Slack faz ao tirar o `<@U…>` do começo do app_mention."""
    text = activity.get("text", "") or ""
    return _AT_TAG.sub("", text).strip()


def is_mention(activity: dict[str, Any]) -> bool:
    """True se a Activity menciona o bot (tag `<at>` no texto OU uma entity de
    mention cujo mentioned.id casa com o recipient/bot). Outgoing webhooks
    sempre @mencionam o bot; o canal Bot Framework usa entities."""
    if _AT_TAG.search(activity.get("text", "") or ""):
        return True
    recipient_id = (activity.get("recipient") or {}).get("id")
    for ent in activity.get("entities", []) or []:
        if ent.get("type") == "mention":
            mentioned = (ent.get("mentioned") or {}).get("id")
            if recipient_id is None or mentioned == recipient_id:
                return True
    return False


def thread_key(activity: dict[str, Any]) -> str:
    return conversation_id(activity)


def event_kind(activity: dict[str, Any]) -> EventKind:
    """Menção ao bot -> task_request; mensagem comum numa conversa existente ->
    clarification_answer (mesma convenção do Slack para mensagem em thread; o
    gate de steering em `correlate` trata review/steering)."""
    return EventKind.task_request if is_mention(activity) else EventKind.clarification_answer


def compute_event_id(activity: dict[str, Any]) -> str:
    """event_id determinístico (defesa #4). Puro — não depende da ativação."""
    return ConversationEvent.compute_event_id(
        PLATFORM_STR, thread_key(activity), activity_id(activity)
    )


def source_ref(activity: dict[str, Any]) -> dict[str, str]:
    return {"conversation_id": conversation_id(activity)}


def build_conversation_event(
    activity: dict[str, Any], *, resolved_principal: str, sanitized_text: str | None = None
) -> ConversationEvent:
    """Constrói o `ConversationEvent` tipado. Levanta `TeamsNotActivated` se a
    fundação ainda não expõe `Platform.teams` (bloqueio de ativação). O
    `content_snapshot` é o texto ORIGINAL congelado (TOCTOU); `sanitized_text`,
    quando fornecido, é aplicado pelo gateway em `admit`/`record_signal`, não aqui.
    """
    user_id, display = actor_of(activity)
    return ConversationEvent.build(
        platform=teams_platform(),
        thread_key=thread_key(activity),
        message_id=activity_id(activity),
        kind=event_kind(activity),
        source_ref=source_ref(activity),
        actor=Actor(platform_user_id=user_id, resolved_principal=resolved_principal, display_name=display),
        content_snapshot=clean_text(activity),
        signature_verified=True,
    )
