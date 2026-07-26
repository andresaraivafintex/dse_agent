"""WSA-E6-T1 — Path A/B correlation.

For an incoming `ConversationEvent`, `correlate(...)` decides whether it opens
a new task (Path A, "new_task" -> `admit_work_item`) or is a signal to a task
already in flight (Path B, "signal" -> `SignalWorkflow`, decided by the
caller: the dispatcher itself or WS-B via the Temporal client).

Deterministic lookup by `source_ref` (thread_ts/PR number/ticket) against
`work_items` with a NON-terminal status. `source_ref` convention used for the
match (see adapters): Slack `{"channel":..., "thread_ts":...}`, GitHub
`{"repo":..., "number":...}` (the same number covers issue and PR — the GitHub
API shares the number namespace between them).

Steering allowlist rule (WSA-E6-T2a): a comment of kind `steering` or
`review_comment` on an active task only becomes a "signal" if
`is_authorized_to_steer` authorizes the principal; otherwise it returns
"unauthorized" and emits
`dse_audit.emit(action="steering_rejected_unauthorized")`.
`clarification_answer`/`approval` do not go through this gate — they are
expected replies within the flow itself (the bot asked, the user answered),
not an unsolicited injection of direction.

Event correlated to a WorkItem already in a TERMINAL state (done/failed): by
definition it cannot receive a signal (the workflow has already ended) — the
documented rule is to allow creating a NEW WorkItem with a provenance link to
the previous one (`provenance_work_item_id`), recorded by the caller in the
`details` of the admission audit row.
"""
from __future__ import annotations

import json
from typing import Any, Literal, NamedTuple

from dse_audit import emit as audit_emit
from dse_contracts import ConversationEvent, EventKind, WorkItemStatus

from .steering import is_authorized_to_steer

CorrelationKind = Literal["new_task", "signal", "unauthorized"]

_TERMINAL_STATUSES = {WorkItemStatus.done.value, WorkItemStatus.failed.value}

# Kinds that represent "someone injecting new direction" into an active task —
# they go through the steering allowlist gate (deny-by-default; see steering.py).
#
# Plan 08 §F (F4): `clarification_answer` is ALSO gated. On a public GitHub
# issue (or channel/ticket) ANYONE can comment; an unauthorized third party
# answering the clarification would inject direction into the task without
# going through authorization. The legitimate requester is on the allowlist
# (seeded with requester + CODEOWNERS — see steering.py), so the expected flow
# does not break; a stranger becomes `steering_rejected_unauthorized` (audited,
# does not signal). Plan `approval` has its OWN gate (approver resolution,
# WSB-E3-T2) and is not duplicated here.
_STEERING_GATED_KINDS = {
    EventKind.steering,
    EventKind.review_comment,
    EventKind.clarification_answer,
}


class CorrelationResult(NamedTuple):
    kind: CorrelationKind
    work_item_id: str | None
    provenance_work_item_id: str | None = None


def correlate(
    conn,
    *,
    tenant_id: str,
    event: ConversationEvent,
    requester_principal: str,
    correlation_ref: dict[str, Any] | None = None,
) -> CorrelationResult:
    """Transaction note: when the result is "unauthorized", this function
    writes the audit row using `conn` but does NOT commit — the caller
    (adapter) owns the transaction boundary and must call `conn.commit()` (same
    convention as `dse_audit.emit(conn=...)`)."""
    ref = correlation_ref if correlation_ref is not None else event.source_ref

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, status, requester FROM work_items
            WHERE tenant_id = %s AND source_ref @> %s::jsonb
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (tenant_id, json.dumps(ref)),
        )
        row = cur.fetchone()

    if row is None:
        return CorrelationResult("new_task", None)

    matched_id, status, wi_requester = row

    if status in _TERMINAL_STATUSES:
        # Documented rule: a terminal WorkItem does not receive a signal — it
        # becomes a new WorkItem with provenance to the previous one.
        return CorrelationResult("new_task", None, provenance_work_item_id=matched_id)

    if event.kind in _STEERING_GATED_KINDS:
        # Plan 08 §F (F4, audit adjustment): the task's REQUESTER answering the
        # clarification of their OWN task is the expected flow (the question was
        # asked to them) — authorized by construction, a deterministic
        # comparison against the requester column (P1). Applies only to
        # clarification_answer; steering/review_comment from anyone (including
        # the requester) still go through the strict gate. Without this, F4
        # would block the real flow on all three channels (Jira/Slack treat
        # every comment as a clarification_answer).
        if (
            event.kind is EventKind.clarification_answer
            and wi_requester
            and requester_principal == wi_requester
        ):
            audit_emit(
                actor=requester_principal,
                action="steering_authorized",
                tenant_id=tenant_id,
                work_item_id=matched_id,
                details={"method": "task_requester", "kind": event.kind.value},
                conn=conn,
            )
            return CorrelationResult("signal", matched_id)
        if not is_authorized_to_steer(tenant_id, requester_principal):
            audit_emit(
                actor=requester_principal,
                action="steering_rejected_unauthorized",
                tenant_id=tenant_id,
                work_item_id=matched_id,
                details={"event_id": event.event_id, "kind": event.kind.value, "source_ref": ref},
                conn=conn,
            )
            return CorrelationResult("unauthorized", matched_id)

    return CorrelationResult("signal", matched_id)
