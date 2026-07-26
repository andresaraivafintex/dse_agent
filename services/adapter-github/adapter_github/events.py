"""Normalization of GitHub webhooks -> `ConversationEvent` (WSA-E4-T1).

`source_ref` is normalized as `{"repo": <owner/repo>, "number": <N>}` — the
same number covers both issue and PR (the GitHub API shares the namespace),
which lets `correlate()` match a `pull_request_review_comment` or an
`issue_comment` on a PR with the WorkItem originally opened by the issue (or
vice versa) with no extra logic.

TOCTOU defense (WSA-E2-T2): `content_snapshot` is read straight from the
webhook body (`payload["comment"]["body"]` / issue title+body) — never
re-fetched via `GET /repos/.../issues/{n}` afterwards.
"""
from __future__ import annotations

from typing import Any

from dse_contracts import Actor, ConversationEvent, EventKind, Platform


def _actor(login: str, resolved_principal: str) -> Actor:
    return Actor(platform_user_id=login, resolved_principal=resolved_principal, display_name=login)


def is_pull_request_comment(issue_payload: dict[str, Any]) -> bool:
    """`issue_comment` events carry `issue.pull_request` when the comment is
    actually on a PR (GitHub treats a PR as an issue)."""
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
    """Returns `(event, is_pr_comment)`. `is_pr_comment=True` -> the caller
    (app.py) must NEVER call `admit_work_item` for this event (WSA-E4:
    'must create ZERO new WorkItems'), only `correlate`+signal."""
    repo = payload["repository"]["full_name"]
    number = payload["issue"]["number"]
    sender = payload["comment"]["user"]["login"]
    comment_id = payload["comment"]["id"]
    body = payload["comment"]["body"]

    is_pr_comment = is_pull_request_comment(payload["issue"])
    # Default for a plain issue: clarification_answer (an ordinary reply/comment)
    # — app.py promotes it to task_request if the body mentions the bot.
    # PR: always review_comment (never creates a new WorkItem, see WSA-E4-T1).
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


def build_event_from_pr_merged(
    payload: dict[str, Any], *, resolved_principal: str, merge_sha: str
) -> ConversationEvent:
    """WSA-E4-T3 — `pull_request` webhook (action=closed, merged=true) ->
    `ConversationEvent` for human merge approval. `merge_sha` (the PR's
    `merge_commit_sha`) goes into the `message_id` so that the deterministic
    `event_id` dedups redeliveries of the same merge webhook.

    Only built when `pull_request.merged == true` (the handler in app.py has
    already filtered the PR-closed-WITHOUT-merge case — which triggers NOTHING,
    a documented route)."""
    repo = payload["repository"]["full_name"]
    number = payload["pull_request"]["number"]
    return ConversationEvent.build(
        platform=Platform.github,
        thread_key=f"{repo}:{number}",
        message_id=f"merged:{merge_sha}",
        kind=EventKind.approval,
        source_ref={"repo": repo, "number": number},
        actor=_actor(resolved_principal, resolved_principal),
        content_snapshot=f"pull_request #{number} merged",
        signature_verified=True,
    )


def build_event_from_pr_review(payload: dict[str, Any], *, resolved_principal: str) -> ConversationEvent:
    """`pull_request_review` webhook (action=submitted) — the ONLY GitHub event
    that carries the formal review verdict (`review.state`: approved /
    changes_requested / commented). Post-S7 audit: without this builder,
    `source_ref.review_state` was never populated and `_review_signal_payload`
    (dispatcher) returned None for EVERY GitHub review — the changes_requested
    loop was unreachable from the UI. `review_state` goes into the source_ref
    and becomes the signal's verdict."""
    repo = payload["repository"]["full_name"]
    number = payload["pull_request"]["number"]
    review = payload["review"]
    sender = review["user"]["login"]

    return ConversationEvent.build(
        platform=Platform.github,
        thread_key=f"{repo}:{number}",
        message_id=f"review:{review['id']}",
        kind=EventKind.review_comment,
        source_ref={"repo": repo, "number": number, "review_state": str(review.get("state", "")).lower()},
        actor=_actor(sender, resolved_principal),
        content_snapshot=review.get("body") or "",
        signature_verified=True,
    )


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
