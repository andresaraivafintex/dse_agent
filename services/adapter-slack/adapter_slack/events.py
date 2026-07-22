"""Normalização Slack Events API / Interactivity -> `ConversationEvent`
(WSA-E3-T1). Passa pelas 4 defesas de intake ANTES de chegar aqui (a
verificação de assinatura acontece em `app.py`, que só chama estas funções
depois de `signature_verified=True`).

Defesa TOCTOU (WSA-E2-T2): `content_snapshot` é o texto exatamente como
veio no corpo do webhook — nunca re-buscamos a mensagem via
`conversations.history`/`conversations.replies` depois. Isso é, por
construção, automático aqui: só lemos `event["text"]` do payload recebido.
"""
from __future__ import annotations

from typing import Any

from dse_contracts import Actor, ConversationEvent, EventKind, Platform


def _actor_from_user_id(user_id: str, resolved_principal: str, display_name: str | None = None) -> Actor:
    return Actor(platform_user_id=user_id, resolved_principal=resolved_principal, display_name=display_name)


def build_event_from_app_mention(event: dict[str, Any], *, resolved_principal: str) -> ConversationEvent:
    channel = event["channel"]
    ts = event["ts"]
    thread_ts = event.get("thread_ts", ts)  # sem thread_ts -> esta msg é a raiz de uma thread nova
    return ConversationEvent.build(
        platform=Platform.slack,
        thread_key=f"{channel}:{thread_ts}",
        message_id=ts,
        kind=EventKind.task_request,
        source_ref={"channel": channel, "thread_ts": thread_ts},
        actor=_actor_from_user_id(event["user"], resolved_principal),
        content_snapshot=event.get("text", ""),
        signature_verified=True,
    )


def build_event_from_thread_message(event: dict[str, Any], *, resolved_principal: str) -> ConversationEvent:
    """Mensagem comum (sem menção) dentro de uma thread existente. Fase 1:
    tratada como `clarification_answer` por padrão — desambiguar
    clarification vs. steering exigiria saber se o bot está aguardando
    resposta, estado que vive no workflow do WS-B, não no adapter
    (documentado como limitação conhecida no README)."""
    channel = event["channel"]
    ts = event["ts"]
    thread_ts = event["thread_ts"]  # só chamado quando thread_ts está presente (ver app.py)
    return ConversationEvent.build(
        platform=Platform.slack,
        thread_key=f"{channel}:{thread_ts}",
        message_id=ts,
        kind=EventKind.clarification_answer,
        source_ref={"channel": channel, "thread_ts": thread_ts},
        actor=_actor_from_user_id(event["user"], resolved_principal),
        content_snapshot=event.get("text", ""),
        signature_verified=True,
    )


_REJECT_TOKENS = ("reject", "rejected", "deny", "denied", "changes", "re_plan", "replan")


def parse_slack_approval(action_id: str, value: str) -> tuple[str, str | None]:
    """C1 (relatório 07): deriva verdict/route DETERMINÍSTICO do clique de botão.
    O verdict NÃO pode ficar só no texto — tem que virar marcador
    (`approval_verdict`/`approval_route`) que o dispatcher lê, senão o default
    é `approved` e um "reject" aprovaria silenciosamente (bug de segurança do
    gate). Espelha o caminho do Jira (`ingest_status_approval`).

    Convenção dos botões (postados no Block Kit de aprovação): `action_id` ou
    `value` contendo 'reject'/'deny'/'re_plan' => rejected; o `value` pode
    carregar a rota após ':' (ex.: 'reject:re_plan'). Qualquer outra coisa =>
    approved (fail-safe: uma rejeição jamais é lida como aprovação, mas um
    clique ambíguo NÃO auto-aprova algo destrutivo — só segue o fluxo de
    aprovação normal, que ainda exige o gate)."""
    haystack = f"{action_id}|{value}".lower()
    is_reject = any(tok in haystack for tok in _REJECT_TOKENS)
    if not is_reject:
        return "approved", None
    # rota da rejeição: parte após ':' no value, senão default re_plan.
    route = "re_plan"
    if ":" in value:
        candidate = value.split(":", 1)[1].strip()
        if candidate:
            route = candidate
    return "rejected", route


def build_event_from_block_action(payload: dict[str, Any], *, resolved_principal: str) -> ConversationEvent:
    """Interação de botão (`block_actions`) -> kind=approval. `source_ref`
    usa o canal+thread_ts da mensagem original de status (onde o botão
    apareceu) para correlacionar de volta ao WorkItem certo."""
    channel = payload["channel"]["id"]
    message = payload.get("message", {})
    thread_ts = message.get("thread_ts", message.get("ts"))
    action = payload["actions"][0]
    action_ts = payload.get("action_ts", action.get("action_ts", "0"))
    user_id = payload["user"]["id"]

    action_id = action.get("action_id", "unknown_action")
    value = action.get("value", "")

    return ConversationEvent.build(
        platform=Platform.slack,
        thread_key=f"{channel}:{thread_ts}",
        message_id=action_ts,
        kind=EventKind.approval,
        source_ref={"channel": channel, "thread_ts": thread_ts},
        actor=_actor_from_user_id(user_id, resolved_principal),
        content_snapshot=f"button:{action_id}={value}",
        signature_verified=True,
    )
