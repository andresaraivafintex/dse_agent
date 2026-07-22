"""Normalização Jira -> `ConversationEvent` (WSA-E5-T1).

`source_ref` normalizado como `{"ticket_key": "DSE-123"}` — correlação por
ticket key (o mesmo issue serve para task, clarificação, aprovação de plano).

Defesa TOCTOU (WSA-E2-T2): `content_snapshot` vem direto do payload recebido
(webhook) ou do estado do issue retornado pela busca (poller) — nunca é
re-buscado depois.

IMPORTANTE — idempotência webhook×poller (WSA-E5-T2): os `message_id` são
derivados do ESTADO do issue (id do issue + status/coluna, id do comentário),
NUNCA do id do changelog do webhook. Assim o webhook (best-effort) e o poller
(que só vê o estado atual, sem changelog) produzem o MESMO `event_id`
determinístico para o mesmo fato — reentrega por qualquer das duas vias
deduplica via a UNIQUE constraint de `ingest_events.event_id`, nunca
duplicando.
"""
from __future__ import annotations

from typing import Any

from dse_contracts import Actor, ConversationEvent, EventKind, Platform


def ticket_key(issue: dict[str, Any]) -> str:
    return issue["key"]


def project_key(issue: dict[str, Any]) -> str:
    """Usado como `channel` do kill switch/admissão (granularidade por projeto
    Jira). Deriva de `fields.project.key` ou do prefixo da ticket key."""
    proj = (issue.get("fields", {}).get("project") or {}).get("key")
    if proj:
        return proj
    key = issue.get("key", "")
    return key.split("-", 1)[0] if "-" in key else key


def issue_labels(issue: dict[str, Any]) -> list[str]:
    return list(issue.get("fields", {}).get("labels") or [])


def issue_type(issue: dict[str, Any]) -> str | None:
    """Nome do issue type do Jira (Bug/Story/Task/...) — plano 08 §A: alimenta
    a classificação determinística de task_class na admissão."""
    return (issue.get("fields", {}).get("issuetype") or {}).get("name")


def first_component(issue: dict[str, Any]) -> str | None:
    """Nome do 1º Component do Jira (C2/relatório 07): Components mapeiam issues
    a subsistemas/serviços — o sinal de repo de granularidade mais fina do Jira.
    None se o ticket não tem component."""
    comps = issue.get("fields", {}).get("components") or []
    for c in comps:
        name = (c or {}).get("name")
        if name:
            return name
    return None


def issue_status_name(issue: dict[str, Any]) -> str:
    return (issue.get("fields", {}).get("status") or {}).get("name", "")


def has_trigger_label(issue: dict[str, Any], trigger_label: str) -> bool:
    return trigger_label in issue_labels(issue)


def _actor(account_id: str, resolved_principal: str, display_name: str | None) -> Actor:
    return Actor(platform_user_id=account_id, resolved_principal=resolved_principal, display_name=display_name)


def _issue_content(issue: dict[str, Any]) -> str:
    fields = issue.get("fields", {})
    summary = fields.get("summary", "") or ""
    description = fields.get("description")
    # API v3 devolve description como ADF (dict); poller/webhook podem trazer
    # texto simples. Só concatena quando é string (o texto real do usuário).
    desc_text = description if isinstance(description, str) else ""
    return f"{summary}\n\n{desc_text}".strip()


def build_task_event(
    issue: dict[str, Any], *, actor_account_id: str, resolved_principal: str, display_name: str | None = None
) -> ConversationEvent:
    """Issue marcado com a trigger label -> task_request. `message_id`
    derivado do id do issue (estado), então criar+rotular+poller convergem no
    mesmo event_id."""
    return ConversationEvent.build(
        platform=Platform.jira,
        thread_key=ticket_key(issue),
        message_id=f"created:{issue['id']}",
        kind=EventKind.task_request,
        source_ref={"ticket_key": ticket_key(issue)},
        actor=_actor(actor_account_id, resolved_principal, display_name),
        content_snapshot=_issue_content(issue),
        signature_verified=True,
    )


def build_comment_event(
    *,
    key: str,
    comment_id: str,
    body: str,
    actor_account_id: str,
    resolved_principal: str,
    display_name: str | None = None,
) -> ConversationEvent:
    """Comentário no issue -> clarification_answer por padrão (mesma
    convenção do adapter-slack para mensagem comum em thread; `correlate`
    decide Path A/B, e o gate de steering trata review/steering). `message_id`
    é o id do comentário — o poller busca comentários e vê o mesmo id."""
    return ConversationEvent.build(
        platform=Platform.jira,
        thread_key=key,
        message_id=f"comment:{comment_id}",
        kind=EventKind.clarification_answer,
        source_ref={"ticket_key": key},
        actor=_actor(actor_account_id, resolved_principal, display_name),
        content_snapshot=body,
        signature_verified=True,
    )


def build_status_approval_event(
    issue: dict[str, Any],
    *,
    target_status: str,
    actor_account_id: str,
    resolved_principal: str,
    display_name: str | None = None,
) -> ConversationEvent:
    """Transição de status para a coluna de aprovação configurada -> kind=
    approval (UC5 na superfície Jira, WSA-E5-T1). `message_id` derivado de
    (issue id, status alvo) — estado, não changelog — para o poller reconstruir
    o mesmo event_id."""
    return ConversationEvent.build(
        platform=Platform.jira,
        thread_key=ticket_key(issue),
        message_id=f"status:{issue['id']}:{target_status}",
        kind=EventKind.approval,
        source_ref={"ticket_key": ticket_key(issue)},
        actor=_actor(actor_account_id, resolved_principal, display_name),
        content_snapshot=f"status transition -> {target_status}",
        signature_verified=True,
    )
