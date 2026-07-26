"""WSE-E6-T15 — workflow resume from a review comment (core of UC4).

Takes a `ConversationEvent` already correlated and verified by WS-A plus the
resolved `work_item_id` (the correlation "which work_item_id does this
comment/PR belong to" is WS-A's responsibility — see README §Cross-workstream),
translates the CONTENT into a human decision (`approved` | `changes_requested`)
and signals the workflow via `signal_workflow` using the SAME workflow_id
(=work_item_id) that WS-B expects (WSB-E3-T4).

P1 (deterministic-or-human): the interpretation uses only structured fields that
WS-A has already normalized (`EventKind.approval`, or `source_ref["review_state"]`
for formal GitHub reviews) — never an LLM. A "loose" `review_comment` (with no
formal approval/changes state) is not a decision: it signals nothing, creates no
WorkItem, creates no PR — it just returns `False`.
"""
from __future__ import annotations

from typing import Literal, Protocol

from dse_contracts import ConversationEvent, EventKind
from dse_contracts.constants import SIGNAL_REVIEW_COMMENT

try:
    from dse_audit import emit as audit_emit
except ImportError:  # pragma: no cover
    audit_emit = None

# Found during Phase 1 integration: this module used its own local constant
# ("review_decision"), which did not match WS-B's real `@workflow.signal def
# review_comment(...)` — the signal never reached the handler. Fixed to import
# the canonical name from dse_contracts.constants (the same constant that WS-A's
# dispatcher now uses for kind=review_comment).
REVIEW_DECISION_SIGNAL_NAME = SIGNAL_REVIEW_COMMENT

ReviewDecision = Literal["approved", "changes_requested"]

_CHANGES_REQUESTED_STATES = {"changes_requested", "request_changes", "changes-requested"}
_APPROVED_STATES = {"approved"}


class WorkflowHandle(Protocol):
    async def signal(self, name: str, arg: object | None = None) -> None: ...


class TemporalClientLike(Protocol):
    def get_workflow_handle(self, workflow_id: str) -> WorkflowHandle: ...


def interpret_review_decision(event: ConversationEvent) -> ReviewDecision | None:
    """Deterministic, no LLM. `EventKind.approval` always becomes "approved".
    `EventKind.review_comment` only becomes a decision if WS-A has already
    attached an explicit `review_state` to `source_ref` (formal GitHub review
    state: APPROVED/CHANGES_REQUESTED) — a plain review comment (`review_state`
    absent or "commented") is not a decision."""
    if event.kind == EventKind.approval:
        return "approved"
    if event.kind == EventKind.review_comment:
        review_state = str(event.source_ref.get("review_state", "")).lower()
        if review_state in _CHANGES_REQUESTED_STATES:
            return "changes_requested"
        if review_state in _APPROVED_STATES:
            return "approved"
        return None
    return None


async def handle_review_event(
    event: ConversationEvent,
    work_item_id: str,
    tenant_id: str,
    temporal_client: TemporalClientLike,
    actor: str | None = None,
) -> bool:
    """Returns True if it signaled the workflow; False if the event did not carry
    a review decision — in that case NO side effect happens (no new WorkItem, no
    new PR, no signal)."""
    decision = interpret_review_decision(event)
    if decision is None:
        return False

    handle = temporal_client.get_workflow_handle(work_item_id)
    resolved_actor = actor or event.actor.resolved_principal or f"platform_user:{event.actor.platform_user_id}"
    # Found during Phase 1 integration: the workflow (services/orchestrator/src/
    # dse_orchestrator/workflows.py) reads `payload["verdict"]` and
    # `payload["comment"]` — this module sent `"decision"` (a different key) and
    # no `"comment"`, so the verdict was never read correctly (it fell into the
    # "unknown_review_verdict" branch and escalated). Fixed to the real format
    # the workflow consumes.
    payload = {
        "verdict": decision,
        "comment": event.content_snapshot,
        "event_id": event.event_id,
        "actor": resolved_actor,
    }
    await handle.signal(REVIEW_DECISION_SIGNAL_NAME, payload)

    if audit_emit is not None:
        audit_emit(
            actor=resolved_actor,
            action="review_decision_signaled",
            tenant_id=tenant_id,
            work_item_id=work_item_id,
            details=payload,
        )
    return True
