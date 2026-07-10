"""Normalização de webhooks GitHub -> `ConversationEvent` (WSA-E4-T1).

`source_ref` normalizado como `{"repo": <owner/repo>, "number": <N>}` —
o mesmo número serve para issue e PR (a API do GitHub compartilha o
namespace), o que permite `correlate()` casar um `pull_request_review_comment`
ou `issue_comment` numa PR com o WorkItem aberto originalmente pela issue
(ou vice-versa) sem lógica extra.

Defesa TOCTOU (WSA-E2-T2): `content_snapshot` é lido diretamente do corpo do
webhook (`payload["comment"]["body"]` / título+corpo da issue) — nunca
re-buscado via `GET /repos/.../issues/{n}` depois.
"""
from __future__ import annotations

from typing import Any

from dse_contracts import Actor, ConversationEvent, EventKind, Platform


def _actor(login: str, resolved_principal: str) -> Actor:
    return Actor(platform_user_id=login, resolved_principal=resolved_principal, display_name=login)


def is_pull_request_comment(issue_payload: dict[str, Any]) -> bool:
    """`issue_comment` events carregam `issue.pull_request` quando o
    comentário é, na verdade, num PR (GitHub trata PR como issue)."""
    return "pull_request" in issue_payload


def build_event_from_issue_assigned_or_labeled(
    payload: dict[str, Any], *, delivery_id: str, resolved_principal: str
) -> ConversationEvent:
    repo = payload["repository"]["full_name"]
    number = payload["issue"]["number"]
    sender = payload["sender"]["login"]
    title = payload["issue"].get("title", "")
    body = payload["issue"].get("body") or ""
    action = payload["action"]

    return ConversationEvent.build(
        platform=Platform.github,
        thread_key=f"{repo}:{number}",
        message_id=f"{action}:{delivery_id}",
        kind=EventKind.task_request,
        source_ref={"repo": repo, "number": number},
        actor=_actor(sender, resolved_principal),
        content_snapshot=f"{title}\n\n{body}".strip(),
        signature_verified=True,
    )


def build_event_from_issue_comment(
    payload: dict[str, Any], *, resolved_principal: str
) -> tuple[ConversationEvent, bool]:
    """Retorna `(event, is_pr_comment)`. `is_pr_comment=True` -> o caller
    (app.py) NUNCA deve chamar `admit_work_item` para este evento (WSA-E4:
    'deve criar ZERO WorkItems novos'), só `correlate`+signal."""
    repo = payload["repository"]["full_name"]
    number = payload["issue"]["number"]
    sender = payload["comment"]["user"]["login"]
    comment_id = payload["comment"]["id"]
    body = payload["comment"]["body"]

    is_pr_comment = is_pull_request_comment(payload["issue"])
    # Default para issue comum: clarification_answer (resposta/comentário
    # comum) — app.py promove para task_request se o corpo mencionar o bot.
    # PR: sempre review_comment (nunca cria WorkItem novo, ver WSA-E4-T1).
    kind = EventKind.review_comment if is_pr_comment else EventKind.clarification_answer

    event = ConversationEvent.build(
        platform=Platform.github,
        thread_key=f"{repo}:{number}",
        message_id=str(comment_id),
        kind=kind,
        source_ref={"repo": repo, "number": number},
        actor=_actor(sender, resolved_principal),
        content_snapshot=body,
        signature_verified=True,
    )
    return event, is_pr_comment


def build_event_from_pr_review_comment(payload: dict[str, Any], *, resolved_principal: str) -> ConversationEvent:
    repo = payload["repository"]["full_name"]
    number = payload["pull_request"]["number"]
    sender = payload["comment"]["user"]["login"]
    comment_id = payload["comment"]["id"]
    body = payload["comment"]["body"]

    return ConversationEvent.build(
        platform=Platform.github,
        thread_key=f"{repo}:{number}",
        message_id=str(comment_id),
        kind=EventKind.review_comment,
        source_ref={"repo": repo, "number": number},
        actor=_actor(sender, resolved_principal),
        content_snapshot=body,
        signature_verified=True,
    )
