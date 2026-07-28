"""WSB-E2/E3/E5 — `WorkItemLifecycleWorkflow`: the WorkItem state machine
(proposal §9.3), Phase 1 (no separate Planner/Tester/Reviewer, no plan approval
gate by risk class, no fairness/budget — that is Phase 2 / WSB-E4).

Flow: new -> needs_clarification (capped rounds) -> ready -> queued ->
implementing <-> validating (capped fix loop) -> pr_ready <-> review_feedback
(human loop, no automatic merge on ANY path) -> done. blocked/failed/escalated
are terminal and never "guess" the next step.

Determinism discipline (P1): all I/O (Postgres, audit, Coder, L1, PR, CI) lives
in an Activity. The `@workflow.run` body only does: counter arithmetic,
string/enum comparison, `workflow.wait_condition`, and calls to
`workflow.execute_activity`/`workflow.continue_as_new`.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from datetime import timedelta
from typing import Any, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from dse_contracts.activities import (
        ACTIVITY_CHECKPOINT_SANDBOX,
        ACTIVITY_CONSUME_CI_STATUS,
        ACTIVITY_EMIT_AUDIT,
        ACTIVITY_FINALIZE_PR,
        ACTIVITY_POST_TRACKING_COMMENT,
        ACTIVITY_PROVISION_SANDBOX,
        ACTIVITY_REBUILD_SANDBOX,
        ACTIVITY_RUN_CODER_TURN,
        ACTIVITY_RUN_DEMO_EVIDENCE,
        ACTIVITY_RUN_L1_PIPELINE,
        ACTIVITY_RUN_L2_REVIEW,
        ACTIVITY_RUN_PLANNER_TURN,
        ACTIVITY_RUN_TESTER_TURN,
        ACTIVITY_RUN_VISUAL_DIFF,
        ACTIVITY_TEARDOWN_SANDBOX,
        ACTIVITY_TRIGGER_PREVIEW,
        ACTIVITY_UPDATE_BASE_BRANCH,
        ACTIVITY_VERIFY_MERGE_STATE,
        CheckpointRef,
        CiStatusResult,
        CoderTurnResult,
        DemoEvidenceResult,
        L1Result,
        L2Verdict,
        GateStatus,
        MergeVerification,
        PreviewRef,
        PrRef,
        SandboxHandle,
        TesterTurnResult,
        UpdateBaseBranchResult,
        VisualDiffResult,
    )
    from dse_contracts.constants import WORKFLOW_TYPE
    from dse_contracts.failure import FAIL_CLOSED_CLASSES, FailureClass, parse_failure_type
    from dse_contracts.plan_artifact import PlanArtifact
    from dse_contracts.work_item import MergedByHumanSignal, WorkItemStatus

    from dse_orchestrator import policy
    from dse_orchestrator.local_activities import (
        LOCAL_ACTIVITY_CHECK_CLARIFICATION,
        LOCAL_ACTIVITY_EMIT_HISTORY_METRIC,
        LOCAL_ACTIVITY_EMIT_PR_QUALITY_METRIC,
        LOCAL_ACTIVITY_LOAD_WORK_ITEM,
        LOCAL_ACTIVITY_POST_STATUS_TRANSITION,
        LOCAL_ACTIVITY_PREVIEW_ENABLED,
        LOCAL_ACTIVITY_RECORD_EVIDENCE,
        LOCAL_ACTIVITY_RECORD_GATE,
        LOCAL_ACTIVITY_RECORD_SKILL_EPISODE,
        LOCAL_ACTIVITY_RESOLVE_APPROVER,
        LOCAL_ACTIVITY_RESOLVE_BUDGET_CAP,
        LOCAL_ACTIVITY_UPDATE_STATUS,
    )
    from dse_orchestrator.models import (
        PHASE_IMPLEMENTATION,
        PHASE_INTAKE,
        PHASE_REVIEW,
        OperatorEvent,
        WorkItemLifecycleInput,
        WorkItemLifecycleResult,
    )

logger = logging.getLogger("dse_orchestrator.workflow")

_SYSTEM_ACTOR = "system:orchestrator"
_MAX_OPERATOR_EVENTS = 25

# Phase 2 — new internal status of the plan approval gate (WSB-E3-T2). It is NOT
# in the `dse_contracts.work_item.WorkItemStatus` enum (foundation — not
# editable by this workstream), but the `work_items.status` column is TEXT
# without a CHECK, and the foundation's own `constants.py` already references
# this string as the status value the WS-A dispatcher queries to route
# SIGNAL_PLAN_APPROVAL. Gap documented in the README ("`awaiting_plan_approval`
# state — documented enum gap"): the foundation enum should gain this member
# (+ public map -> "blocked").
STATUS_AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"

# Phase 2 (plan 09): the PRIMARY classification is structured — the raiser
# declares the class in the ApplicationError `type` (dse.failure.* vocabulary +
# legacy; see dse_contracts.failure and _failure_class_from). This substring
# list is ONLY the fallback for histories/raisers predating the vocabulary —
# retire it together with the "structured-failure-codes-v1" patch marker in the
# next release.
_FAIL_CLOSED_MARKERS = (
    "egress",            # egress-proxy unavailable -> zero egress (fail-closed)
    "fail_closed",
    "fail-closed",
    "policy_denied",
    "budget_denied",
    "virtual_key_expired",
    "key_expired",
    "kill_switch",
    # finding from a real run (2026-07-22): exhausted provider credits are NOT a
    # transient failure — the CLI masked it as "error result: success" and the
    # activity retried forever. Classified non-retryable at the source
    # (sandbox_runtime) and recognized here -> clean failure commented on the issue.
    "provider_billing",
    "credit balance",
)


# Where the reminder lands when the configured one does not fit inside the
# deadline. Half the window is the least surprising reading of two numbers that
# contradict each other: the ping keeps real lead time and the deadline itself is
# never moved.
_REMINDER_FALLBACK_FRACTION = 0.5


def _approval_windows(
    reminder_hours: float, timeout_hours: float
) -> tuple[timedelta, timedelta, bool]:
    """Splits the approval deadline into (window before the reminder, window
    after it, reminder_was_overridden) — the same two-window shape the
    clarification gate computes inline. Pure arithmetic over values carried in
    the workflow INPUT, so it is replay-safe: no `time.time()`, no wall clock,
    nothing read from the environment inside the sandbox.

    A reminder is only useful STRICTLY INSIDE the deadline. Clamping it to the
    deadline (the first version of this function) satisfied the arithmetic and
    broke the promise the docstring made: a plausible misconfiguration —
    reminder 24h, timeout 12h — fired the ping and the escalation in the SAME
    instant, so the human was formally "pinged" and lost the item in the same
    second. A reminder that does not fit (too late, zero, or negative) therefore
    falls back to half the deadline, and the third element of the tuple says it
    happened, so the caller can log the misconfiguration and record the
    EFFECTIVE value instead of pretending the configured one was used.
    """
    total = timedelta(hours=max(timeout_hours, 0.0))
    reminder = timedelta(hours=max(reminder_hours, 0.0))
    if not timedelta(0) < reminder < total:
        reminder = total * _REMINDER_FALLBACK_FRACTION
        return reminder, total - reminder, True
    return reminder, total - reminder, False


def _failure_class_from(exc: Exception) -> "FailureClass | None":
    """Walks the Activity error's cause chain reading the ApplicationError
    `type` (canonical dse.failure.* or legacy) — deterministic and I/O-free
    (safe inside the workflow sandbox). Depth is capped."""
    cause: Exception | None = exc
    for _ in range(6):
        if cause is None:
            return None
        parsed = parse_failure_type(getattr(cause, "type", None))
        if parsed is not None:
            return parsed
        cause = getattr(cause, "cause", None) or getattr(cause, "__cause__", None)
    return None


class _CancelledByOperator(Exception):
    pass


class _ForceClarification(Exception):
    def __init__(self, reason: str):
        self.reason = reason


class _EscalateNow(Exception):
    def __init__(self, reason: str):
        self.reason = reason


class _FailClosed(Exception):
    """Policy refusal on the model/egress path (WSB-E5-T3b) — clean, audited
    failure, with no truncated output (P6)."""

    def __init__(self, reason: str):
        self.reason = reason


class _BudgetExhausted(Exception):
    """WSB-E4-T1 — budget ceiling reached at a BOUNDARY (never mid-Activity)."""

    def __init__(self, reason: str):
        self.reason = reason


class _PlanRejected(Exception):
    """WSB-E3-T3 — plan rejected; `route` in {re_plan, re_clarify, cancel}."""

    def __init__(self, route: str, actor: str, justification: str):
        self.route = route
        self.actor = actor
        self.justification = justification


class _BlockNow(Exception):
    """WSB-E3-T2 — EMPTY approver cascade: Blocked + escalation, it never
    auto-approves on absence (P1/P3)."""

    def __init__(self, reason: str):
        self.reason = reason


class _ActivityRetriesExhausted(Exception):
    """A business Activity exhausted its cap; the workflow projects a terminal failure."""

    def __init__(self, activity_name: str, reason: str):
        self.activity_name = activity_name
        self.reason = reason


@workflow.defn(name=WORKFLOW_TYPE)
class WorkItemLifecycleWorkflow:
    def __init__(self) -> None:
        self._input: Optional[WorkItemLifecycleInput] = None

        # --- operator controls (WSB-E5-T2) ---
        self._paused = False
        self._cancelled = False
        self._cancel_reason: str | None = None
        self._force_clarification_requested = False
        self._force_clarification_reason: str | None = None
        self._operator_escalate_requested = False
        self._operator_escalate_reason: str | None = None
        self._retry_from_checkpoint_requested = False
        self._model_override: str | None = None
        self._runtime_override: str | None = None
        self._operator_log: list[OperatorEvent] = []

        # --- business signals (WSB-E2-T4 / E3-T1 / E3-T4) ---
        self._clarification_received = False
        self._clarification_payload: dict[str, Any] | None = None
        self._review_received = False
        self._review_payload: dict[str, Any] | None = None
        # Phase 3 (WSB-E4-T2/ADR-26): review comments ACCUMULATE in a list
        # (instead of a single payload) so the review loop consumes them in a
        # BATCH — N comments within a debounce window become ONE fix cycle +
        # ONE evidence refresh, never N.
        self._review_comments: list[dict[str, Any]] = []
        self._merged = False
        self._merge_payload: dict[str, Any] | None = None
        # Phase 3 (ADR-26): EXPLICIT human request for an evidence refresh —
        # the only refresh trigger besides a fix cycle (new commit).
        self._refresh_evidence_requested = False

        # --- Phase 2: plan approval gate (WSB-E3-T2/T3) ---
        self._plan_approval_received = False
        self._plan_approval_payload: dict[str, Any] | None = None
        # --- Phase 2: operator-driven budget retry (WSB-E4-T1) ---
        self._budget_raise_requested = False
        self._budget_new_max: float | None = None

        # --- Phase 4: detection of a RECURRING clarification gap (source of the
        # skill-learning input, WSC-E4-T2). Union of the fields that were
        # already missing in previous rounds of THIS intake run — if a field
        # shows up missing again after we already asked for clarification, that
        # is a recurrence and becomes a skill_episode. Instance state
        # (replay-safe: deterministically rebuilt from the completeness
        # Activities' results during replay); it does not need to cross
        # continue_as_new because the intake loop completes within one run.
        self._clarification_missing_union: set[str] = set()

    # ------------------------------------------------------------------
    # Signals — WSB-E2-T4, WSB-E3-T1, WSB-E3-T4, WSB-E5-T2
    # ------------------------------------------------------------------
    @workflow.signal
    def clarification_answer(self, payload: dict[str, Any]) -> None:
        self._clarification_payload = payload
        self._clarification_received = True

    @workflow.signal
    def review_comment(self, payload: dict[str, Any]) -> None:
        # Phase 3: accumulate (do not overwrite) — the review loop consumes the
        # whole BATCH at once (ADR-26 debounce). `_review_payload` still keeps
        # the last one, for observability/query compatibility.
        self._review_comments.append(payload)
        self._review_payload = payload
        self._review_received = True

    @workflow.signal
    def refresh_evidence(self, payload: dict[str, Any] | None = None) -> None:
        """Phase 3 (ADR-26) — EXPLICIT human request to regenerate evidence
        (preview/demo/visual diff) without a new commit. Together with "a fix
        cycle ran", it is the ONLY refresh trigger — review comments on their
        own NEVER regenerate evidence (100%% deterministic decision, P1)."""
        self._refresh_evidence_requested = True

    @workflow.signal
    def merged_by_human(self, payload: dict[str, Any] | None = None) -> None:
        self._merge_payload = payload or {}
        self._merged = True

    @workflow.signal(name="plan_approval")
    def plan_approval(self, payload: dict[str, Any]) -> None:
        """WSB-E3-T2/T3 — plan approval gate verdict. The name matches
        `dse_contracts.SIGNAL_PLAN_APPROVAL`. Payload: {"verdict":
        "approved"|"rejected", "route": "re_plan"|"re_clarify"|"cancel"
        (required when rejected), "comment"/"justification": str,
        "actor": resolved principal}. The WS-A dispatcher only routes
        `kind=approval` here when work_items.status ==
        'awaiting_plan_approval'. We do not reset the flag in the handler (same
        anti-clobber discipline as the other signals)."""
        self._plan_approval_payload = payload
        self._plan_approval_received = True

    @workflow.signal
    def raise_budget(self, new_max_usd: float) -> None:
        """WSB-E4-T1 — the operator raises the budget ceiling. Applied at the
        NEXT check boundary (it never interrupts a running Activity, P6). Lets a
        WorkItem that hit (or is about to hit) the ceiling resume without
        starting over."""
        self._budget_raise_requested = True
        self._budget_new_max = float(new_max_usd)
        self._log_operator("raise_budget", str(new_max_usd))

    @workflow.signal
    def pause(self, reason: str | None = None) -> None:
        self._paused = True
        self._log_operator("pause", reason)

    @workflow.signal
    def resume(self, reason: str | None = None) -> None:
        self._paused = False
        self._log_operator("resume", reason)

    @workflow.signal
    def cancel(self, reason: str | None = None) -> None:
        self._cancelled = True
        self._cancel_reason = reason
        self._log_operator("cancel", reason)

    @workflow.signal
    def retry_from_checkpoint(self, reason: str | None = None) -> None:
        self._retry_from_checkpoint_requested = True
        self._log_operator("retry_from_checkpoint", reason)

    @workflow.signal
    def force_clarification(self, reason: str | None = None) -> None:
        self._force_clarification_requested = True
        self._force_clarification_reason = reason
        self._log_operator("force_clarification", reason)

    @workflow.signal
    def escalate(self, reason: str | None = None) -> None:
        self._operator_escalate_requested = True
        self._operator_escalate_reason = reason
        self._log_operator("escalate", reason)

    @workflow.signal
    def reassign_model(self, model: str) -> None:
        self._model_override = model
        self._log_operator("reassign_model", model)

    @workflow.signal
    def reassign_runtime(self, runtime: str) -> None:
        self._runtime_override = runtime
        self._log_operator("reassign_runtime", runtime)

    def _log_operator(self, action: str, reason: str | None) -> None:
        self._operator_log.append(OperatorEvent(action=action, actor="operator", reason=reason))
        if len(self._operator_log) > _MAX_OPERATOR_EVENTS:
            self._operator_log.pop(0)

    # ------------------------------------------------------------------
    # Queries — observability/operator
    # ------------------------------------------------------------------
    @workflow.query
    def get_status(self) -> str:
        return self._input.status if self._input else "unknown"

    @workflow.query
    def get_state(self) -> dict[str, Any]:
        if self._input is None:
            return {}
        state = dataclasses.asdict(self._input)
        state["paused"] = self._paused
        state["cancelled"] = self._cancelled
        return state

    @workflow.query
    def get_operator_log(self) -> list[dict[str, Any]]:
        return [dataclasses.asdict(e) for e in self._operator_log]

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    @workflow.run
    async def run(self, raw_input: Any) -> WorkItemLifecycleResult:
        input = await self._coerce_input(raw_input)
        self._input = input
        try:
            if input.phase == PHASE_INTAKE:
                return await self._run_intake_phase()
            elif input.phase == PHASE_IMPLEMENTATION:
                return await self._run_implementation_phase()
            elif input.phase == PHASE_REVIEW:
                return await self._run_review_phase()
            else:
                # terminal phase already resolved (defensive — should not happen)
                return WorkItemLifecycleResult(
                    work_item_id=input.work_item_id,
                    status=input.status,
                    detail=input.terminal_detail,
                    pr_number=input.pr_number,
                )
        except _CancelledByOperator:
            return await self._finish_cancelled()
        except _EscalateNow as exc:
            return await self._finish_escalated(exc.reason)
        except _FailClosed as exc:
            # WSB-E5-T3b — policy refusal on the model/egress path: clean,
            # audited failure, with no truncated output (P6).
            return await self._finish_failed(f"fail_closed:{exc.reason}",
                                             audit_action="model_path_fail_closed")
        except _BudgetExhausted as exc:
            return await self._finish_failed(f"budget_exhausted:{exc.reason}",
                                             audit_action="budget_exhausted")
        except _PlanRejected as exc:
            return await self._handle_plan_rejection(exc)
        except _BlockNow as exc:
            return await self._finish_blocked(exc.reason, audit_action="plan_gate_blocked_no_approver")
        except _ActivityRetriesExhausted as exc:
            return await self._finish_failed(
                f"activity_retries_exhausted:{exc.activity_name}:{exc.reason}",
                audit_action="activity_retries_exhausted",
            )
        except _ForceClarification as exc:
            input.phase = PHASE_INTAKE
            input.status = WorkItemStatus.needs_clarification.value
            input.clarification_rounds = 0
            input.terminal_detail = f"force_clarification: {exc.reason}"
            await self._audit("force_clarification_applied", {"reason": exc.reason})
            await self._persist_status()
            return await self._continue_as_new(input)

    # ------------------------------------------------------------------
    # Generic helpers
    # ------------------------------------------------------------------
    async def _coerce_input(self, raw_input: Any) -> WorkItemLifecycleInput:
        """Integration robustness (real finding while testing against the WS-A
        worker): whoever starts the workflow may call `StartWorkflow` with just
        the `work_item_id` (string) instead of the full
        `WorkItemLifecycleInput` — e.g. `client.start_workflow(WORKFLOW_TYPE,
        work_item_id, id=work_item_id, task_queue=...)`. The internal
        continue_as_new ALWAYS passes the full dataclass, so this branch only
        runs on the FIRST `run()` of an externally started execution. See the
        README, section "Assumed start_workflow contract"."""
        if isinstance(raw_input, WorkItemLifecycleInput):
            return raw_input
        if isinstance(raw_input, dict):
            return WorkItemLifecycleInput(**raw_input)
        if isinstance(raw_input, str):
            row = await workflow.execute_activity(
                LOCAL_ACTIVITY_LOAD_WORK_ITEM,
                {"work_item_id": raw_input},
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
            budget = row.get("budget") or {}
            max_usd = budget.get("max_usd") if isinstance(budget, dict) else None
            return WorkItemLifecycleInput(
                work_item_id=row["work_item_id"],
                tenant_id=row["tenant_id"],
                requester=row["requester"],
                repo=row.get("repo"),
                base_branch=row.get("base_branch"),
                data_class=row.get("data_class") or "internal",
                pr_number=row.get("pr_number"),
                risk_class=row.get("risk_class") or "low",
                plan_json=row.get("plan") or {},
                plan_hash=row.get("plan_hash"),
                base_sha=row.get("base_sha"),
                head_sha=row.get("head_sha"),
                pr_url=row.get("pr_url"),
                ci_status=row.get("ci_status"),
                state_version=int(row.get("state_version") or 0),
                task_content=row.get("task_content") or "",
                issue_number=(int(row["issue_number"]) if row.get("issue_number") else None),
                budget_max_usd=float(max_usd) if max_usd is not None else None,
            )
        raise ApplicationError(
            f"WorkItemLifecycleWorkflow.run got an unsupported input type: {type(raw_input)!r}"
        )

    # Both helpers are pure string formatting over values already in hand — no
    # Activity, no clock, no randomness — so they are safe to call from inside
    # the workflow and deterministic on replay (P1).
    @staticmethod
    def _tester_failure_context(tester_result) -> list[str]:
        """What the Coder needs to know about a suite that just failed."""
        output = (getattr(tester_result, "failure_output", "") or "").strip()
        if not output:
            # An older worker returns no output. Say so plainly rather than
            # implying the suite was silent — the two call for different fixes.
            return [
                "The test suite failed (exit code "
                f"{getattr(tester_result, 'returncode', '?')}) and its output was not captured. "
                "Re-read the tests and the code under test, and look for the mismatch."
            ]
        return [
            "The test suite you must satisfy is FAILING. Fix the code so it passes; "
            "do not weaken or delete the tests.\n"
            f"Exit code: {getattr(tester_result, 'returncode', '?')}\n"
            f"Output:\n{output}"
        ]

    @staticmethod
    def _l1_failure_context(failed_checks) -> list[str]:
        """The L1 checks that did not pass, with the reason each gave."""
        lines = []
        for f in failed_checks:
            detail = (getattr(f, "detail", "") or "").strip()
            status = getattr(getattr(f, "status", None), "value", None) or "FAIL"
            lines.append(f"- {f.check} [{status}]: {detail or 'no detail reported'}")
        if not lines:
            return []
        return [
            "The L1 validation pipeline rejected the previous attempt. "
            "Fix these, and change nothing else:\n" + "\n".join(lines)
        ]

    def _agent_instruction(self, *, include_objections: bool = False) -> str:
        """S1 (Phase 5): builds the REAL instruction the Planner/Coder receive —
        the task description (issue title+body), plus acceptance criteria and
        clarification answers, plus (for the Coder in the fix loop) the L2
        objections. Deterministic (P1): it only concatenates already-captured
        strings, no LLM decides the content. Before S1 the agents only got
        `clarification_notes` (empty) and did not know the task."""
        input = self._input
        parts: list[str] = []
        if (input.task_content or "").strip():
            parts.append(input.task_content.strip())
        if (input.acceptance_criteria or "").strip():
            parts.append(f"Acceptance criteria: {input.acceptance_criteria.strip()}")
        for note in input.clarification_notes:
            if note and note.strip():
                parts.append(f"Clarification: {note.strip()}")
        if include_objections:
            for obj in getattr(input, "l2_objections", []) or []:
                if obj and str(obj).strip():
                    parts.append(f"Review objection to fix: {str(obj).strip()}")
        # Why the previous attempt failed. Last, so it is the freshest thing the
        # Coder reads, and unconditional: it is only ever non-empty while a
        # retry is pending, so the first turn is unaffected.
        for note in getattr(input, "fix_context", []) or []:
            if note and str(note).strip():
                parts.append(str(note).strip())
        return "\n\n".join(parts) or "Implement the requested task."

    def _pr_summary(self) -> str:
        """S7 (Phase 5): PR body (the `summary` field of FinalizePrInput).
        Deterministic (P1): uses the plan's `summary` when present, otherwise
        the 1st line of the task content. No LLM decides here."""
        input = self._input
        plan_summary = None
        if isinstance(input.plan_json, dict):
            plan_summary = (input.plan_json.get("summary") or "").strip() or None
        title = ""
        if (input.task_content or "").strip():
            title = input.task_content.strip().splitlines()[0].strip()
        return plan_summary or title or f"DSE: automated change ({input.work_item_id})"

    @staticmethod
    def _l1_evidence(l1_result) -> str:
        """S7 follow-up: test evidence in the PR body (P8 — evidence over
        assertion). At this point the real evidence that EXISTS is the L1 result
        (the video/preview pipeline runs AFTER the PR); it deterministically
        summarizes the checks that passed. Without this the PR went out with
        "(no evidence link)"."""
        if l1_result is None or not getattr(l1_result, "findings", None):
            return ""
        checks = ", ".join(f"{f.check} ✓" for f in l1_result.findings if f.passed)
        return f"L1 green ({checks})" if checks else ""

    async def _boundary_gate(self) -> None:
        """Checked before EVERY business Activity (not before local
        bookkeeping). A pause never interrupts a running Activity — it only
        delays the next call (WSB-E5-T2)."""
        await workflow.wait_condition(lambda: (not self._paused) or self._cancelled)
        if self._cancelled:
            raise _CancelledByOperator()
        if self._operator_escalate_requested:
            raise _EscalateNow(self._operator_escalate_reason or "operator_escalate")
        if self._force_clarification_requested:
            raise _ForceClarification(self._force_clarification_reason or "operator_requested")

    async def _audit(self, action: str, details: dict[str, Any] | None = None) -> None:
        payload = {
            "actor": _SYSTEM_ACTOR,
            "action": action,
            "tenant_id": self._input.tenant_id,
            "work_item_id": self._input.work_item_id,
            "details": details or {},
        }
        await workflow.execute_activity(
            ACTIVITY_EMIT_AUDIT,
            payload,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )

    async def _persist_status(self, *, clear_ci_status: bool = False) -> None:
        # The patch marker preserves replay of histories whose old payload had
        # only work_item_id/status/pr_number. New executions project the whole
        # operational truth; the server derives plan_hash/expected_files.
        if workflow.patched("operational-state-payload-v1"):
            payload = {
                "work_item_id": self._input.work_item_id,
                "status": self._input.status,
                "pr_number": self._input.pr_number,
                "pr_url": self._input.pr_url,
                "plan": self._input.plan_json or None,
                "risk_class": self._input.risk_class,
                "base_sha": self._input.base_sha,
                "head_sha": self._input.head_sha,
                "ci_status": self._input.ci_status,
                "clear_ci_status": clear_ci_status,
                "last_error": self._input.terminal_detail,
                "validation_attempts": {
                    "coder": self._input.coder_retry_count,
                    "l2": self._input.l2_retry_count,
                    "review": self._input.review_round,
                    "ci_pending": self._input.ci_pending_polls,
                },
            }
        else:  # replay of the historical payload
            payload = {
                "work_item_id": self._input.work_item_id,
                "status": self._input.status,
                "pr_number": self._input.pr_number,
            }
        persisted = await workflow.execute_activity(
            LOCAL_ACTIVITY_UPDATE_STATUS,
            payload,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )
        if isinstance(persisted, dict):
            self._input.state_version = int(
                persisted.get("state_version") or self._input.state_version
            )
            self._input.plan_hash = persisted.get("plan_hash") or self._input.plan_hash

    async def _set_status(self, status: WorkItemStatus, *, audit_action: str | None = None,
                           details: dict[str, Any] | None = None,
                           clear_ci_status: bool = False) -> None:
        self._input.status = status.value
        await self._persist_status(clear_ci_status=clear_ci_status)
        if audit_action:
            await self._audit(audit_action, details)

    async def _set_raw_status(self, status: str, *, audit_action: str | None = None,
                              details: dict[str, Any] | None = None) -> None:
        """Like `_set_status`, but for a status value that is not in the
        foundation's `WorkItemStatus` enum (e.g. `awaiting_plan_approval` — see
        STATUS_AWAITING_PLAN_APPROVAL and the README). The column is TEXT and
        accepts the string; the enum gap is documented in the README."""
        self._input.status = status
        await self._persist_status()
        if audit_action:
            await self._audit(audit_action, details)

    def _activity_timeouts(self) -> dict[str, Any]:
        return dict(
            start_to_close_timeout=timedelta(seconds=self._input.activity_start_to_close_seconds),
            heartbeat_timeout=timedelta(seconds=self._input.activity_heartbeat_seconds),
            schedule_to_close_timeout=timedelta(seconds=self._input.activity_schedule_to_close_seconds),
        )

    async def _teardown_sandbox_best_effort(self, stage: str) -> None:
        """The ONE place a sandbox is released. Best-effort by construction: a
        teardown failure must never change the terminal state the caller already
        decided on.

        Callers on a path that EXISTS in closed histories (cancel, fail) call
        this unguarded — the command stream has to stay byte-identical to what
        those histories recorded, which is why the activity name, the three
        payload keys and both timeouts below are frozen. Callers on a path that
        never tore down before (escalate, block) must wrap the call in the
        `teardown-on-escalation-v1` patch guard.

        Boundary fix (post-S7 audit) preserved verbatim: TeardownSandboxInput
        requires tenant_id — without it the decode failed and NO teardown ran
        (orphaned sandboxes). `reason` becomes `stage` (the model's real field).
        """
        if not self._input.sandbox_id:
            return
        try:
            await workflow.execute_activity(
                ACTIVITY_TEARDOWN_SANDBOX,
                {
                    "work_item_id": self._input.work_item_id,
                    "tenant_id": self._input.tenant_id,
                    "stage": stage,
                },
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError:
            logger.warning("teardown failed at stage=%s; continuing anyway", stage)

    async def _finish_cancelled(self) -> WorkItemLifecycleResult:
        await self._teardown_sandbox_best_effort("cancelled_by_operator")
        detail = f"cancelled_by_operator: {self._cancel_reason or 'no reason given'}"
        self._input.terminal_detail = detail
        await self._set_status(
            WorkItemStatus.failed,
            audit_action="cancelled_by_operator",
            details={"reason": self._cancel_reason},
        )
        await self._post_status_comment("failed", detail=detail)
        return WorkItemLifecycleResult(
            work_item_id=self._input.work_item_id,
            status=WorkItemStatus.failed.value,
            detail=detail,
            pr_number=self._input.pr_number,
        )

    async def _post_status_comment(self, status: str, *, detail: str = "") -> None:
        """Cheap oversight (never leave a surface without the current state):
        every consequential transition reflects the status on the originating
        surface's single comment. BEST-EFFORT — it never blocks the transition
        (the audit ledger is the source of truth; the comment is convenience).
        The Activity resolves repo/issue and is an audited no-op for non-github
        sources (Slack/Jira will get their own outbound path with the SAME
        status vocabulary)."""
        # Replay guard (spec §5): the status surface was added later — old
        # histories do NOT have this Activity and must replay without it. ONE
        # marker covers ALL transitions (terminal + implementing); new
        # executions post, old replays skip deterministically.
        if not workflow.patched("status-comment-surfacing-v1"):
            return
        try:
            await workflow.execute_activity(
                ACTIVITY_POST_TRACKING_COMMENT,
                {"work_item_id": self._input.work_item_id, "tenant_id": self._input.tenant_id,
                 "status": status, "detail": detail},
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
        except ActivityError:
            logger.warning("best-effort post_status_comment failed (status=%s)", status)
        # Phase B: also reflect on the BOARD (Jira column transition). No-op for
        # github/slack. New guard (its own patch) for replay safety — histories
        # in between the status surface and the board skip only this part.
        if workflow.patched("status-transition-board-v1"):
            try:
                await workflow.execute_activity(
                    LOCAL_ACTIVITY_POST_STATUS_TRANSITION,
                    {"work_item_id": self._input.work_item_id,
                     "tenant_id": self._input.tenant_id, "status": status},
                    start_to_close_timeout=timedelta(seconds=60),
                    retry_policy=RetryPolicy(maximum_attempts=5),
                )
            except ActivityError:
                logger.warning("best-effort post_status_transition failed (status=%s)", status)

    async def _post_board_transition(self, status: str) -> None:
        """Moves the Jira card to the mapped column, WITHOUT re-posting the
        status comment. It exists because `_post_status_comment` was only called
        on `implementing` and on error states — the card moved to In Progress
        but NEVER to In Review (PR open) or Done (merge). Finding BD-39
        (2026-07-23). Best-effort, no-op for github/slack (the Activity resolves
        the source), with its own patch guard for replay safety of in-flight
        workflows."""
        if not workflow.patched("board-transition-pr-and-done-v1"):
            return
        try:
            await workflow.execute_activity(
                LOCAL_ACTIVITY_POST_STATUS_TRANSITION,
                {"work_item_id": self._input.work_item_id,
                 "tenant_id": self._input.tenant_id, "status": status},
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
        except ActivityError:
            logger.warning("best-effort post_board_transition failed (status=%s)", status)

    async def _exhaust_ci_wait(self, cause: str, *, elapsed_seconds: float = 0.0) -> None:
        """ONE audit row at a real state transition — never one per timer tick.

        Its whole purpose is to be greppable. Before this, exhausting the CI
        wait raised into the generic escalation handler, which wrote
        `action="escalated"` with the cause buried inside `details.reason`, left
        `ci_status` reading "pending" on a terminal row, and had no test
        anywhere. An operator had no way to distinguish "the CI gate gave up"
        from any other escalation. The caller raises `_EscalateNow` immediately
        after; this only names the cause."""
        await self._audit("ci_wait_exhausted", {
            "reason": f"ci_wait_exhausted:{cause}",
            "status": self._input.ci_status,
            "pr_number": self._input.pr_number,
            "polls": self._input.ci_pending_polls,
            "poll_cap": self._input.ci_pending_poll_cap,
            "elapsed_seconds": int(elapsed_seconds),
            "deadline_hours": self._input.ci_wait_deadline_hours,
            "head_sha": self._input.head_sha,
        })

    async def _finish_escalated(self, reason: str) -> WorkItemLifecycleResult:
        # Escalation is TERMINAL for this execution — nothing will ever exec
        # into this sandbox again — and it was the ONLY terminal path without a
        # teardown, while being the path every stalled review lands on: the
        # single `except _EscalateNow` in run() funnels every escalate raise
        # here, including _checkpoint_or_rebuild("done"), which is called one
        # line before the merge-path teardown. That is how eight work items
        # that opened a PR ended `checkpointed` with the Pod still Running.
        #
        # PATCH GUARD (spec §5, same discipline as status-comment-surfacing-v1):
        # this schedules a NEW command on a path that closed histories ran with
        # no command at all. Without the guard, replaying a closed escalated
        # history — which Temporal does on a query against a closed workflow and
        # on a reset — is nondeterministic. Old histories skip; new executions
        # tear down.
        if workflow.patched("teardown-on-escalation-v1"):
            await self._teardown_sandbox_best_effort(f"escalated:{reason[:64]}")
        detail = f"escalated: {reason}"
        self._input.terminal_detail = detail
        await self._set_status(
            WorkItemStatus.escalated,
            audit_action="escalated",
            details={"reason": reason},
        )
        await self._post_status_comment("escalated", detail=reason)
        # Phase 4 — if a PR had already been finalized, this escalation is a
        # terminal PR boundary: emit the quality metric (pilot gate) so we do
        # not lose data on PRs that never merge. Pre-PR escalations (e.g.
        # clarification) have no PR and are skipped.
        if self._input.pr_finalized_at_epoch is not None:
            await self._emit_pr_quality_metric("escalated")
        return WorkItemLifecycleResult(
            work_item_id=self._input.work_item_id,
            status=WorkItemStatus.escalated.value,
            detail=detail,
            pr_number=self._input.pr_number,
        )

    async def _finish_failed(self, reason: str, *, audit_action: str) -> WorkItemLifecycleResult:
        """CLEAN terminal failure (P6): Failed status + audit, with no truncated
        output. Tears down the sandbox if one exists (best-effort)."""
        await self._teardown_sandbox_best_effort(audit_action)
        detail = reason
        self._input.terminal_detail = detail
        await self._set_status(
            WorkItemStatus.failed, audit_action=audit_action, details={"reason": reason}
        )
        await self._post_status_comment("failed", detail=reason)
        return WorkItemLifecycleResult(
            work_item_id=self._input.work_item_id,
            status=WorkItemStatus.failed.value,
            detail=detail,
            pr_number=self._input.pr_number,
        )

    async def _finish_blocked(self, reason: str, *, audit_action: str) -> WorkItemLifecycleResult:
        """Blocked terminal state (WSB-E3-T2: empty approver cascade). Distinct
        from Failed — the WorkItem is waiting for human intervention (naming an
        approver), not dead. It escalates alongside (audit)."""
        # Blocked is terminal too. Today it is unreachable WITH a sandbox — the
        # only _BlockNow raise lives in _run_planner_and_gate, which the
        # implementation phase calls only under `if not input.sandbox_id` — so
        # this guards against the gate ever moving after provision; it is not a
        # live leak. Same patch id as the escalate path: one marker, one
        # decision, both new terminal teardowns.
        if workflow.patched("teardown-on-escalation-v1"):
            await self._teardown_sandbox_best_effort(f"blocked:{audit_action}")
        detail = reason
        self._input.terminal_detail = detail
        await self._set_status(
            WorkItemStatus.blocked, audit_action=audit_action, details={"reason": reason}
        )
        await self._audit("escalated", {"reason": reason})
        await self._post_status_comment("blocked", detail=reason)
        return WorkItemLifecycleResult(
            work_item_id=self._input.work_item_id,
            status=WorkItemStatus.blocked.value,
            detail=detail,
            pr_number=self._input.pr_number,
        )

    # ------------------------------------------------------------------
    # Budgets (WSB-E4-T1) — deterministic and pure (arithmetic/comparison),
    # safe in the workflow body. Checked at admission and at EVERY boundary.
    # ------------------------------------------------------------------
    def _apply_pending_budget_raise(self) -> None:
        if self._budget_raise_requested and self._budget_new_max is not None:
            self._input.budget_max_usd = self._budget_new_max
            self._budget_raise_requested = False
            self._budget_new_max = None

    async def _budget_boundary(self, boundary: str) -> None:
        """Checks the budget ceiling at a BOUNDARY. If a pending raise was
        applied, it uses the new ceiling. Exceeded -> `_BudgetExhausted` (it
        never cuts mid-Activity — only here, between them). Every check is
        audited."""
        self._apply_pending_budget_raise()
        # Fall back to the deployment default when the work item carries no
        # ceiling of its own — which is every item today, since nothing writes
        # `max_usd` into the work_items.budget JSONB. Ordered AFTER the pending
        # raise so an explicit operator raise always wins. Patch-guarded: this
        # adds commands to a path present in every recorded history.
        if (
            self._input.budget_max_usd is None
            and not self._input.budget_default_resolved
            and workflow.patched("default-work-item-budget-v1")
        ):
            await self._resolve_default_budget(boundary)
        cap = self._input.budget_max_usd
        if cap is None:
            return  # no ceiling configured: nothing to enforce
        if self._input.spent_usd >= cap:
            await self._audit(
                "budget_boundary_denied",
                {"boundary": boundary, "spent_usd": round(self._input.spent_usd, 6),
                 "max_usd": cap},
            )
            raise _BudgetExhausted(
                f"boundary={boundary} spent={round(self._input.spent_usd, 6)}>=max={cap}"
            )
        await self._audit(
            "budget_boundary_ok",
            {"boundary": boundary, "spent_usd": round(self._input.spent_usd, 6), "max_usd": cap},
        )

    async def _resolve_default_budget(self, boundary: str) -> None:
        """Reads the deployment default for the per-WorkItem ceiling in an
        Activity — outside the workflow sandbox — so the resolved number lands in
        history and replay stays deterministic across worker versions (P1).
        Only ever called under the `default-work-item-budget-v1` patch guard."""
        try:
            resolved = await workflow.execute_activity(
                LOCAL_ACTIVITY_RESOLVE_BUDGET_CAP,
                {"work_item_id": self._input.work_item_id, "tenant_id": self._input.tenant_id},
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError:
            # run() has no generic `except ActivityError`, so letting this escape
            # would turn a cost guard into an availability incident — the
            # concrete case being a rolling deploy where an old worker without
            # this Activity registered is still polling the same queue. Fail OPEN
            # but LOUD, and retry at the next boundary: budget_default_resolved
            # deliberately stays False.
            logger.warning(
                "resolve_budget_cap failed at boundary=%s; no ceiling applied at this boundary", boundary
            )
            await self._audit("budget_default_unavailable", {"boundary": boundary})
            return
        self._input.budget_default_resolved = True
        max_usd = resolved.get("max_usd")
        if max_usd is None:
            await self._audit("budget_default_disabled", {"boundary": boundary})
            return
        self._input.budget_max_usd = float(max_usd)
        await self._audit(
            "budget_default_applied",
            {"boundary": boundary, "max_usd": self._input.budget_max_usd,
             "source": resolved.get("source", "deployment_default")},
        )

    async def _consume_cost(self, cost_usd: float, *, source: str) -> None:
        """Aggregates the cost reported by the gateway (WS-D) through the
        Activity result. Every consumption is audited (P8)."""
        if not cost_usd:
            return
        self._input.spent_usd += float(cost_usd)
        await self._audit(
            "budget_consumed",
            {"source": source, "delta_usd": float(cost_usd),
             "spent_usd": round(self._input.spent_usd, 6)},
        )

    async def _run_model_activity(self, name: str, payload: dict[str, Any], result_type):
        """Wrapper for the MODEL/SANDBOX path Activities (planner/coder/tester/
        l2/provision/l1). Converts a fail-closed policy refusal (egress-proxy
        down, expired virtual key, kill switch) into `_FailClosed` — a clean,
        audited failure with no truncated output (P6). Retryable/transient
        errors keep being retried by Temporal itself (they do not reach here:
        only the final ActivityError does)."""
        try:
            options = self._activity_timeouts()
            if workflow.patched("model-activity-infra-retry-v2"):
                # spec §6: DISTINGUISH retryable infra failures from permanent
                # ones. Permanent failures (egress/gateway fail-closed, expired
                # virtual key, kill switch, contract decode) are raised
                # `non_retryable` at the source and do NOT retry — they fail
                # fast. The retryable ones (transient gateway flapping) retry
                # under the WALL-CLOCK ceiling (schedule_to_close, ~2h) + the
                # per-phase budget (cost) — never under a COUNT cap that
                # confuses an infra blip with a permanent failure and kills
                # durability (P6/P8: 3 transient outages must not fail an
                # expensive task).
                options["retry_policy"] = RetryPolicy(maximum_attempts=0)
            elif workflow.patched("bounded-business-activity-retries-v1"):
                options["retry_policy"] = RetryPolicy(
                    maximum_attempts=max(1, int(self._input.activity_retry_cap))
                )
            return await workflow.execute_activity(
                name, payload, result_type=result_type, **options
            )
        except ActivityError as exc:
            blob = f"{type(exc.cause).__name__}:{exc.cause}".lower() if exc.cause else str(exc).lower()
            fail_class = None
            if workflow.patched("structured-failure-codes-v1"):
                # Phase 2 (plan 09): the decision comes from the structured
                # ApplicationError `type` — the MESSAGE no longer decides anything.
                fail_class = _failure_class_from(exc)
            if fail_class is None and any(marker in blob for marker in _FAIL_CLOSED_MARKERS):
                # fallback (pre-vocabulary histories/raisers) — retire it in the
                # next release together with _FAIL_CLOSED_MARKERS.
                fail_class = FailureClass.policy_fail_closed
            if fail_class is not None and fail_class in FAIL_CLOSED_CLASSES:
                await self._audit(
                    "model_path_fail_closed_detected",
                    {"activity": name, "failure_class": fail_class.value, "error": blob[:300]},
                )
                raise _FailClosed(f"{name}:{blob[:200]}")
            raise _ActivityRetriesExhausted(name, blob[:300])

    async def _capture_base_sha(self) -> None:
        """Captures the immutable tip prior to the Coder's first turn.

        Reuses the existing deterministic checkpoint: right after provisioning,
        the task branch still points exactly at the base. The returned SHA
        becomes base_sha and the initial head_sha, persisted before any write.
        """
        input = self._input
        try:
            ref: CheckpointRef = await workflow.execute_activity(
                ACTIVITY_CHECKPOINT_SANDBOX,
                {
                    "sandbox_id": input.sandbox_id,
                    "work_item_id": input.work_item_id,
                    "tenant_id": input.tenant_id,
                    "branch": input.branch,
                    "phase": "base",
                },
                result_type=CheckpointRef,
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=RetryPolicy(
                    maximum_attempts=max(1, int(input.activity_retry_cap))
                ),
            )
        except ActivityError as exc:
            raise _ActivityRetriesExhausted(
                ACTIVITY_CHECKPOINT_SANDBOX, str(exc.cause or exc)[:300]
            )
        input.base_sha = ref.git_ref
        input.head_sha = ref.git_ref
        input.last_checkpoint_ref = ref.model_dump()
        if input.sandbox_handle:
            input.sandbox_handle["base_sha"] = input.base_sha
            input.sandbox_handle["head_sha"] = input.head_sha
        await self._persist_status()
        await self._audit("base_sha_captured", {"base_sha": input.base_sha})

    async def _checkpoint_or_rebuild(self, phase_name: str) -> None:
        """WSB-E5-T1: checkpoint at the end of each phase, with bounded retries;
        once exhausted, try a rebuild; if that is exhausted too, escalate (it
        never keeps guessing with a possibly corrupted sandbox)."""
        if not self._input.sandbox_id:
            return
        for _ in range(self._input.checkpoint_retry_cap):
            try:
                # S7 (Phase 5): CheckpointSandboxInput requires `tenant_id` (the
                # old call site did not send it — a latent boundary bug hidden
                # by the fakes; same class as the others). We capture the
                # returned CheckpointRef so the rebuild can restore from it.
                ref: CheckpointRef = await workflow.execute_activity(
                    ACTIVITY_CHECKPOINT_SANDBOX,
                    {
                        "sandbox_id": self._input.sandbox_id,
                        "work_item_id": self._input.work_item_id,
                        "tenant_id": self._input.tenant_id,
                        "phase": phase_name,
                    },
                    result_type=CheckpointRef,
                    start_to_close_timeout=timedelta(seconds=120),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
                self._input.last_checkpoint_ref = ref.model_dump()
                self._input.head_sha = ref.git_ref
                if self._input.sandbox_handle:
                    self._input.sandbox_handle["head_sha"] = ref.git_ref
                return
            except ActivityError:
                continue
        await self._audit("checkpoint_exhausted_attempting_rebuild", {"phase": phase_name})
        # a rebuild is only possible if there is a prior checkpoint to restore from.
        if not self._input.last_checkpoint_ref:
            raise _EscalateNow(f"checkpoint_failed_no_prior_ref:{phase_name}")
        for _ in range(self._input.rebuild_retry_cap):
            try:
                handle: SandboxHandle = await workflow.execute_activity(
                    ACTIVITY_REBUILD_SANDBOX,
                    {"work_item_id": self._input.work_item_id, "tenant_id": self._input.tenant_id,
                     "checkpoint_ref": self._input.last_checkpoint_ref},
                    result_type=SandboxHandle,
                    start_to_close_timeout=timedelta(seconds=300),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
                self._input.sandbox_id = handle.sandbox_id
                self._input.sandbox_handle = handle.model_dump()
                if self._input.base_sha:
                    self._input.sandbox_handle["base_sha"] = self._input.base_sha
                if self._input.head_sha:
                    self._input.sandbox_handle["head_sha"] = self._input.head_sha
                await self._audit("rebuild_succeeded", {"phase": phase_name})
                return
            except ActivityError:
                continue
        raise _EscalateNow(f"checkpoint_and_rebuild_exhausted:{phase_name}")

    async def _continue_as_new(self, input: WorkItemLifecycleInput):
        """Every `continue_as_new` goes through here: it bumps the
        Continue-As-New count (part of the history metric — ALERTING-RULES.md
        §3) and emits the metric one last time before closing the current run."""
        input.continue_as_new_count += 1
        await self._emit_history_metric("continue_as_new")
        return workflow.continue_as_new(input)

    async def _emit_history_metric(self, checkpoint: str) -> None:
        """Phase 3 — feeds the history alert (WS-F enables rule §3).
        DETERMINISTIC read in the workflow (get_current_history_length/size are
        replay-safe in the SDK); OTel emission in the local Activity.
        Best-effort: a metric failure never affects the flow."""
        info = workflow.info()
        payload = {
            "work_item_id": self._input.work_item_id,
            "tenant_id": self._input.tenant_id,
            "phase": self._input.phase,
            "checkpoint": checkpoint,
            "history_length": info.get_current_history_length(),
            "history_size_bytes": info.get_current_history_size(),
            "continue_as_new_count": self._input.continue_as_new_count,
        }
        try:
            await workflow.execute_activity(
                LOCAL_ACTIVITY_EMIT_HISTORY_METRIC,
                payload,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError:
            logger.warning("emit_history_metric failed; the metric is best-effort, the flow continues")

    async def _maybe_retry_from_checkpoint(self) -> None:
        if not self._retry_from_checkpoint_requested:
            return
        self._retry_from_checkpoint_requested = False
        await self._audit("retry_from_checkpoint_applied", {})
        # Boundary fix (post-S7 audit): RebuildSandboxInput requires tenant_id +
        # checkpoint_ref — the old {work_item_id, sandbox_id} payload failed to
        # decode. With no prior checkpoint in this phase, escalate cleanly (P6)
        # instead of rebuilding from nowhere.
        if not self._input.last_checkpoint_ref:
            raise _EscalateNow("retry_from_checkpoint_no_prior_ref")
        try:
            handle: SandboxHandle = await workflow.execute_activity(
                ACTIVITY_REBUILD_SANDBOX,
                {"work_item_id": self._input.work_item_id, "tenant_id": self._input.tenant_id,
                 "checkpoint_ref": self._input.last_checkpoint_ref},
                result_type=SandboxHandle,
                start_to_close_timeout=timedelta(seconds=300),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
            self._input.sandbox_id = handle.sandbox_id
        except ActivityError:
            raise _EscalateNow("retry_from_checkpoint_failed")

    # ------------------------------------------------------------------
    # Phase 1 — intake / clarification gate (WSB-E3-T1)
    # ------------------------------------------------------------------
    async def _run_intake_phase(self) -> WorkItemLifecycleResult:
        input = self._input

        if input.status == WorkItemStatus.new.value:
            await self._audit("intake_started")

        while True:
            await self._boundary_gate()

            completeness = await workflow.execute_activity(
                LOCAL_ACTIVITY_CHECK_CLARIFICATION,
                {
                    "repo": input.repo,
                    "base_branch": input.base_branch,
                    "acceptance_criteria": input.acceptance_criteria,
                    # S2 (Phase 5): the task content joins the checklist — a
                    # well-described issue satisfies the "what to do" without
                    # requiring a separate acceptance_criteria field.
                    "task_content": input.task_content,
                },
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
            if completeness["complete"]:
                await self._set_status(WorkItemStatus.ready, audit_action="clarification_complete")
                input.phase = PHASE_IMPLEMENTATION
                input.status = WorkItemStatus.queued.value
                return await self._continue_as_new(input)

            if input.clarification_rounds >= self._input.clarification_round_cap:
                raise _EscalateNow(
                    f"clarification_round_cap_exhausted (missing={completeness['missing']})"
                )

            # Phase 4 (WSC-E4-T2, source=clarification) — RECURRING gap
            # detection: a field that is still missing AFTER we already asked
            # for clarification at least once in this intake. Pure set
            # arithmetic (deterministic, P1); writing the input lives in an
            # Activity. NO skill is created here — only the episode, which WS-C
            # consumes.
            missing = list(completeness["missing"])
            recurring = sorted(set(missing) & self._clarification_missing_union)
            if recurring:
                await self._emit_clarification_episode(recurring, missing)
            self._clarification_missing_union |= set(missing)

            await self._set_status(
                WorkItemStatus.needs_clarification,
                audit_action="clarification_requested",
                details={"missing": completeness["missing"], "round": input.clarification_rounds},
            )

            # S3 (Phase 5): posts the QUESTION back on the originating surface
            # (GitHub issue), enumerating the missing fields — before this the
            # human never saw what was being asked. Best-effort (never brings
            # the workflow down); the Activity auto-resolves repo/issue_number
            # from the work_item.
            _field_labels = {"acceptance_criteria": "acceptance criteria / expected behavior",
                             "repo": "repository", "base_branch": "base branch"}
            question = "I need the following information before I can start: " + ", ".join(
                _field_labels.get(m, m) for m in completeness["missing"]
            ) + ". Reply in this thread and I will resume automatically."
            # Repo-select dropdown (Slack): when the ONLY missing fields are
            # resolvable by picking the repo (repo and/or base_branch — the repo
            # binding carries both), signal to the adapter (via the comment
            # status) that it may render a static_select. work_items.status
            # stays needs_clarification (the dispatcher routes
            # clarification_answer without looking at the status). Guarded by a
            # patch marker (replay-safe: in-flight histories that already posted
            # with 'needs_clarification' re-execute identically).
            comment_status = "needs_clarification"
            if workflow.patched("repo-select-dropdown-v1") and (
                "repo" in completeness["missing"]
                and set(completeness["missing"]) <= {"repo", "base_branch"}
            ):
                comment_status = "awaiting_repo_selection"
            try:
                await workflow.execute_activity(
                    ACTIVITY_POST_TRACKING_COMMENT,
                    {"work_item_id": input.work_item_id, "tenant_id": input.tenant_id,
                     "status": comment_status, "detail": question},
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
            except ActivityError:
                logger.warning("post_tracking_comment (clarification) failed; the workflow keeps waiting for an answer")

            # IMPORTANT: do not reset `_clarification_received` here — a
            # `clarification_answer` may have already arrived during the awaits
            # above (completeness/status/audit activity); resetting here would
            # erase a legitimate answer (the same "clobber" bug as the review
            # loop — see `_run_review_phase`). We only consume and reset AFTER
            # reading the payload, below.
            reminder_delta = timedelta(hours=self._input.clarification_reminder_hours)
            escalation_delta = timedelta(days=self._input.clarification_escalation_days)
            remaining_after_reminder = escalation_delta - reminder_delta
            if remaining_after_reminder.total_seconds() < 0:
                remaining_after_reminder = timedelta(seconds=0)

            got_answer = await self._wait_with_reminder(
                condition=lambda: self._clarification_received or self._cancelled
                or self._operator_escalate_requested or self._force_clarification_requested,
                reminder_delta=reminder_delta,
                remaining_delta=remaining_after_reminder,
                reminder_action="clarification_reminder_sent",
            )
            if self._cancelled:
                raise _CancelledByOperator()
            if self._operator_escalate_requested:
                raise _EscalateNow(self._operator_escalate_reason or "operator_escalate")
            if self._force_clarification_requested:
                self._force_clarification_requested = False  # we are already in intake

            if not got_answer:
                raise _EscalateNow("clarification_no_response_after_reminder_and_escalation_window")

            payload = self._clarification_payload or {}
            self._clarification_received = False  # consumed — next round waits for a NEW signal
            if payload.get("repo"):
                input.repo = payload["repo"]
            if payload.get("base_branch"):
                input.base_branch = payload["base_branch"]
            if payload.get("acceptance_criteria"):
                input.acceptance_criteria = payload["acceptance_criteria"]
            if payload.get("text"):
                input.clarification_notes.append(payload["text"])

            input.clarification_rounds += 1
            await self._audit("clarification_answer_received", {"round": input.clarification_rounds})
            # back to the top of the loop to re-check completeness (no LLM: pure checklist)

    async def _wait_with_reminder(
        self,
        *,
        condition,
        reminder_delta: timedelta,
        remaining_delta: timedelta,
        reminder_action: str,
        on_reminder=None,
    ) -> bool:
        """`True` if `condition()` became true within the total window
        (reminder + escalation); `False` if the whole deadline elapsed without
        an answer (the caller decides what to do — it never keeps guessing).

        `on_reminder` is an optional coroutine run right after the reminder is
        audited, for gates whose reminder must also show up on the human's
        SURFACE — an audit row is a record, and nobody reads the ledger. (It is
        still not a push notification; see `_post_plan_approval_reminder` for what
        the outbound path can and cannot do.) Callers that do not pass it issue
        exactly the same command sequence as before, which is what keeps the
        clarification gate's in-flight histories replayable without a patch.
        """
        try:
            await workflow.wait_condition(condition, timeout=reminder_delta)
            return True
        except asyncio.TimeoutError:
            pass

        # THE SAME RACE `_expire_plan_approval` RE-CHECKS AT THE DEADLINE, one
        # timer earlier. `wait_condition(timeout=)` reports the timeout even when
        # the signal that satisfies the condition is delivered in the very
        # workflow task that carries the fired timer, so the condition can
        # already be true right here. For the plan gate that meant writing
        # "this plan is still waiting for a decision" over a plan the human had
        # just APPROVED — and on Slack that body lands inside the Block Kit whose
        # Approve/Reject buttons are still live, inviting a second decision on an
        # item already on its way to `implementing`. It also put a
        # `plan_approval_reminder_sent` row in the append-only ledger for a
        # reminder that was not needed.
        #
        # Re-checked only for callers that pass `on_reminder`, which is the exact
        # set whose reminder is SURFACED to a human — and the exact set that is
        # new in this change. The clarification gate has executions parked on
        # this timer right now with no patch marker covering it, and returning
        # early skips its audit Activity and its second timer: two commands its
        # recorded histories contain, so replay would fail with a
        # non-determinism error. Its sequence stays untouched, per the paragraph
        # above.
        if on_reminder is not None and condition():
            return True

        await self._audit(reminder_action)
        if on_reminder is not None:
            await on_reminder()

        if remaining_delta.total_seconds() <= 0:
            return False

        try:
            await workflow.wait_condition(condition, timeout=remaining_delta)
            return True
        except asyncio.TimeoutError:
            return False

    async def _emit_clarification_episode(self, recurring: list[str], missing: list[str]) -> None:
        """Phase 4 (WSC-E4-T2) — writes ONE skill_episode (source=clarification)
        when the same clarification gap recurs. Full provenance
        (repo/fields/round/requester) for WS-C's promotion pipeline to govern.
        NO skill is created here (boundary tested in packages/contracts) — only
        the input. `pattern_key` groups occurrences of the SAME set of recurring
        fields per tenant."""
        input = self._input
        pattern_key = "clarification_missing:" + "+".join(recurring)
        result = await workflow.execute_activity(
            LOCAL_ACTIVITY_RECORD_SKILL_EPISODE,
            {
                "tenant_id": input.tenant_id,
                "source": "clarification",
                "work_item_id": input.work_item_id,
                "pattern_key": pattern_key,
                "provenance": {
                    "repo": input.repo,
                    "recurring_missing": recurring,
                    "missing_now": list(missing),
                    "clarification_round": input.clarification_rounds,
                    "requester": input.requester,
                },
            },
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )
        await self._audit(
            "skill_episode_recorded",
            {"source": "clarification", "pattern_key": pattern_key,
             "recurring_missing": recurring,
             "occurrence_n": (result or {}).get("occurrence_n")},
        )

    # ------------------------------------------------------------------
    # Phase 2 — implementation (extended WSB-E2-T3, WSB-E3-T2/T3, WSB-E4, WSB-E5-T1)
    # Sequence: [budget admission] -> Planner (read-only) -> [plan approval
    # gate] -> provision -> (Coder -> Tester -> L1)* -> L2 -> PR.
    # ------------------------------------------------------------------
    async def _run_implementation_phase(self) -> WorkItemLifecycleResult:
        input = self._input

        # WSB-E4-T1 — budget at admission (never cuts mid-Activity, only here).
        await self._audit(
            "budget_admitted",
            {"max_usd": input.budget_max_usd, "spent_usd": round(input.spent_usd, 6)},
        )
        await self._budget_boundary("admission")

        # Extended WSB-E2-T3 + WSB-E3-T2 — read-only Planner BEFORE the gate,
        # and the approval gate. Runs only while there is no sandbox (once per
        # implementation; re_plan re-enters with sandbox_id None and an empty
        # plan_json).
        if not input.sandbox_id:
            await self._run_planner_and_gate()

        if not input.sandbox_id:
            await self._boundary_gate()
            handle: SandboxHandle = await self._run_model_activity(
                ACTIVITY_PROVISION_SANDBOX,
                {
                    "work_item_id": input.work_item_id,
                    "tenant_id": input.tenant_id,
                    "repo": input.repo,
                    "base_branch": input.base_branch or "main",
                },
                SandboxHandle,
            )
            input.sandbox_id = handle.sandbox_id
            input.branch = handle.branch
            # S7: retain the COMPLETE handle (L1/finalize need it, not just the id).
            input.sandbox_handle = handle.model_dump()
            await self._set_status(WorkItemStatus.implementing, audit_action="sandbox_provisioned",
                                    details={"sandbox_id": handle.sandbox_id})
            # Visible state during the LONGEST phase (coding): without this, an
            # auto-approved low-risk task disappears from the surface between
            # admission and pr_ready. The single comment is edited in-place (no
            # spam). (the replay guard lives inside _post_status_comment.)
            await self._post_status_comment("implementing")
            # New Activity protected by a patch marker: old histories replay
            # without the command; new executions persist the base before the
            # Coder's first turn.
            if workflow.patched("capture-base-sha-before-coder-v1"):
                await self._capture_base_sha()

        while True:
            await self._boundary_gate()
            await self._budget_boundary("coder_turn")
            await self._maybe_retry_from_checkpoint()

            coder_result: CoderTurnResult = await self._run_model_activity(
                ACTIVITY_RUN_CODER_TURN,
                {
                    "sandbox_id": input.sandbox_id,
                    "work_item_id": input.work_item_id,
                    "tenant_id": input.tenant_id,
                    # S1: RunCoderTurnInput requires `instruction` (singular).
                    # The workflow used to send `instructions`/`objections`
                    # (fields the model does not have — dropped at decode); now
                    # the real task + L2 objections are folded into the
                    # instruction.
                    "instruction": self._agent_instruction(include_objections=True),
                    "branch": input.branch,
                    "model_override": self._model_override,
                    "runtime_override": self._runtime_override,
                    # plan anchor: new files outside it are pruned post-turn
                    # (the CLI creates spontaneous reports — real finding)
                    "expected_files": list((input.plan_json or {}).get("expected_files", [])),
                },
                CoderTurnResult,
            )
            await self._consume_cost(coder_result.cost_usd, source="coder")
            await self._audit(
                "coder_turn_completed",
                {"files_changed": coder_result.files_changed, "cost_usd": coder_result.cost_usd},
            )

            # WSB-E2-T3 — Tester session (writes/adjusts tests BEFORE L1).
            await self._boundary_gate()
            await self._budget_boundary("tester_turn")
            tester_result: TesterTurnResult = await self._run_model_activity(
                ACTIVITY_RUN_TESTER_TURN,
                {
                    "sandbox_id": input.sandbox_id,
                    "work_item_id": input.work_item_id,
                    "tenant_id": input.tenant_id,
                    "plan": input.plan_json,
                    "model_override": self._model_override,
                    "runtime_override": self._runtime_override,
                },
                TesterTurnResult,
            )
            await self._consume_cost(tester_result.cost_usd, source="tester")
            await self._audit(
                "tester_turn_completed",
                {
                    "files_changed": tester_result.files_changed,
                    "tests_ran": tester_result.tests_ran,
                    "tests_passed": tester_result.tests_passed,
                    "status": tester_result.status.value,
                },
            )

            if workflow.patched("enforce-tester-result-v1"):
                if not tester_result.tests_ran:
                    return await self._finish_failed(
                        "tester_contract_failed:tests_ran=false",
                        audit_action="tester_tests_not_run",
                    )
                if tester_result.status != GateStatus.PASS:
                    input.coder_retry_count += 1
                    if input.coder_retry_count > input.coder_retry_cap:
                        return await self._finish_failed(
                            "tester_failed_after_retry_cap",
                            audit_action="tester_retry_cap_exhausted",
                        )
                    if workflow.patched("fix-loop-carries-the-failure-v1"):
                        input.fix_context = self._tester_failure_context(tester_result)
                    await self._set_status(
                        WorkItemStatus.implementing,
                        audit_action="tester_failed_retrying",
                        details={"attempt": input.coder_retry_count,
                                 "returncode": tester_result.returncode},
                    )
                    continue

            await self._boundary_gate()
            await self._checkpoint_or_rebuild("implementing")

            await self._set_status(WorkItemStatus.validating, audit_action="l1_started")
            await self._boundary_gate()
            await self._budget_boundary("l1")
            l1_payload = {
                "sandbox": self._input.sandbox_handle,
                "plan": input.plan_json,
                "tenant_id": input.tenant_id,
                "base_branch": input.base_branch or "main",
            }
            if workflow.patched("sha-bound-validation-inputs-v1"):
                l1_payload.update({
                    "work_item_id": input.work_item_id,
                    "base_sha": input.base_sha or "",
                    "head_sha": input.head_sha or "",
                })
            l1_result: L1Result = await self._run_model_activity(
                ACTIVITY_RUN_L1_PIPELINE,
                l1_payload,
                L1Result,
            )
            await self._audit(
                "l1_completed",
                {"passed": l1_result.passed, "findings": [f.check for f in l1_result.findings]},
            )

            l1_passed = (
                l1_result.status == GateStatus.PASS
                if workflow.patched("structured-gate-status-v1")
                else l1_result.passed
            )
            if l1_passed and workflow.patched("fix-loop-carries-the-failure-v1"):
                # Cleared on success, like l2_objections below: a later attempt
                # must never be handed a failure that has already been fixed.
                input.fix_context = []
            if not l1_passed:
                # Finding from a real run (burned ~$8 in useless fix cycles): a
                # missing/broken L1 manifest is a REPO CONFIG problem — no Coder
                # turn fixes that. Escalate with a clear instruction instead of
                # re-running the Coder (P6, cost).
                if workflow.patched("l1-manifest-escalates-v1"):
                    manifest_bad = next(
                        (f for f in l1_result.findings
                         if f.check == "l1_manifest" and not f.passed), None,
                    )
                    if manifest_bad is not None:
                        raise _EscalateNow(
                            "l1_manifest_not_configured: the target repo needs "
                            f".dse/validation.json on the base branch ({manifest_bad.detail[:160]})"
                        )
                input.coder_retry_count += 1
                if input.coder_retry_count > self._input.coder_retry_cap:
                    self._input.terminal_detail = (
                        f"l1_failed_after_{input.coder_retry_count - 1}_retries: "
                        + "; ".join(f"{f.check}:{f.detail}" for f in l1_result.findings if not f.passed)
                    )
                    await self._set_status(WorkItemStatus.failed, audit_action="coder_retry_cap_exhausted")
                    try:
                        await workflow.execute_activity(
                            ACTIVITY_TEARDOWN_SANDBOX,
                            {"work_item_id": input.work_item_id, "tenant_id": input.tenant_id,
                             "stage": "l1_retry_cap_exhausted"},
                            start_to_close_timeout=timedelta(seconds=120),
                            retry_policy=RetryPolicy(maximum_attempts=3),
                        )
                    except ActivityError:
                        logger.warning("teardown failed after retries were exhausted; continuing anyway")
                    return WorkItemLifecycleResult(
                        work_item_id=input.work_item_id,
                        status=WorkItemStatus.failed.value,
                        detail=self._input.terminal_detail,
                        pr_number=input.pr_number,
                    )
                failed_checks = [f for f in l1_result.findings if not f.passed]
                if workflow.patched("fix-loop-carries-the-failure-v1"):
                    input.fix_context = self._l1_failure_context(failed_checks)
                await self._set_status(
                    WorkItemStatus.implementing, audit_action="l1_failed_retrying",
                    # Which checks failed, not how many attempts. `l1_completed`
                    # below records every check that RAN, so a reader could not
                    # tell one broken gate from eight.
                    details={"attempt": input.coder_retry_count,
                             "failed": [f.check for f in failed_checks]},
                )
                continue  # new Coder turn on the same sandbox/branch

            # L1 passed -> fresh-context Reviewer L2 (WSC-E3-T5/WSE-E2).
            # P3: the L2 session gets ONLY the plan artifact + final diff —
            # NEVER the Coder's history/instructions (built in `_run_l2_review`).
            await self._boundary_gate()
            await self._budget_boundary("l2")
            l2: L2Verdict = await self._run_l2_review(coder_result)
            await self._consume_cost(l2.cost_usd, source="l2")

            l2_passed = (
                l2.status == GateStatus.PASS
                if workflow.patched("structured-l2-gate-status-v1")
                else l2.passed
            )
            if not l2_passed:
                input.l2_retry_count += 1
                input.l2_objections = list(l2.objections)
                await self._audit(
                    "l2_objections_raised",
                    {"attempt": input.l2_retry_count, "objections": l2.objections},
                )
                if input.l2_retry_count > self._input.l2_retry_cap:
                    raise _EscalateNow(
                        f"l2_objections_after_retry_cap (objections={l2.objections})"
                    )
                await self._set_status(WorkItemStatus.implementing,
                                        audit_action="l2_changes_requested")
                continue  # back to the Coder with the L2 objections

            input.l2_objections = []  # cleared — L2 approved
            break

        await self._boundary_gate()
        await self._budget_boundary("finalize_pr")
        finalize_payload = {
            "work_item_id": input.work_item_id,
            "tenant_id": input.tenant_id,
            "sandbox": self._input.sandbox_handle,
            "repo": input.repo,
            "base_branch": input.base_branch or "main",
            "branch": input.branch,
            "summary": self._pr_summary(),
            "issue_ref": ({"issue_number": input.issue_number}
                          if input.issue_number else None),
            "evidence_url": self._l1_evidence(l1_result),
        }
        if workflow.patched("sha-bound-finalize-input-v1"):
            finalize_payload.update({
                "base_sha": input.base_sha or "",
                "head_sha": input.head_sha or "",
                "risk_class": input.risk_class,
            })
        try:
            pr_ref: PrRef = await workflow.execute_activity(
            ACTIVITY_FINALIZE_PR,
            finalize_payload,
            result_type=PrRef,
            retry_policy=RetryPolicy(maximum_attempts=max(1, input.activity_retry_cap)),
            **self._activity_timeouts(),
            )
        except ActivityError as exc:
            raise _ActivityRetriesExhausted(
                ACTIVITY_FINALIZE_PR, str(exc.cause or exc)[:300]
            )
        input.pr_number = pr_ref.pr_number
        input.pr_url = pr_ref.url
        input.head_sha = pr_ref.head_sha or input.head_sha
        # Phase 4 — mark the instant the PR was finalized (deterministic/
        # replay-safe workflow.now() read) for the pilot gate's "time to merge".
        if input.pr_finalized_at_epoch is None:
            input.pr_finalized_at_epoch = workflow.now().timestamp()

        await self._boundary_gate()
        await workflow.execute_activity(
            ACTIVITY_POST_TRACKING_COMMENT,
            {
                "work_item_id": input.work_item_id,
                "tenant_id": input.tenant_id,
                "pr_number": pr_ref.pr_number,
                "status": ("pr_open" if workflow.patched("fine-pr-ci-states-v1") else "pr_ready"),
            },
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )

        await self._checkpoint_or_rebuild("pr_open")
        pr_open_status = (
            WorkItemStatus.pr_open
            if workflow.patched("fine-pr-open-state-v1")
            else WorkItemStatus.pr_ready
        )
        await self._set_status(pr_open_status, audit_action="pr_finalized",
                                details={"pr_number": pr_ref.pr_number, "url": pr_ref.url})
        # PR open -> review column on the board (In Review). Without this the
        # card stayed stuck in In Progress even with the PR ready (finding BD-39).
        await self._post_board_transition(pr_open_status.value)

        # Phase 3 — INITIAL evidence pipeline (after finalize_pr):
        # trigger_preview (deterministic paths-filter using CoderTurnResult's
        # files_changed, FR-20) -> if created, run_demo_evidence (preview
        # base_url; the publish happens INSIDE it, WS-E) -> run_visual_diff.
        # Failure/degradation NEVER blocks the PR (failure mode 9) — degraded
        # evidence is recorded and the flow moves on to human review.
        input.last_files_changed = list(coder_result.files_changed)
        await self._run_evidence_pipeline(coder_result.files_changed, reason="initial")
        await self._emit_history_metric("pr_finalized")

        # We do NOT `continue_as_new` here (deliberate — see README.md, section
        # "Determinism discipline (P1) and the \"clobber bug\" this code avoids",
        # on the continue_as_new/signal race): a human/CI can react to the
        # `pr_ready` status (which just became visible via query) so fast that
        # the signal would arrive exactly during the window in which the old run
        # is closing via continue_as_new — and a signal addressed to a closing
        # run can be lost (not delivered to the new run). Continuing in the SAME
        # execution eliminates that race by construction: there is no "closing"
        # run to lose the signal.
        input.phase = PHASE_REVIEW
        return await self._run_review_phase()

    # ------------------------------------------------------------------
    # WSB-E3-T2 — read-only Planner + plan approval gate by risk class
    # ------------------------------------------------------------------
    async def _run_planner_and_gate(self) -> None:
        """Runs the Planner (read-only) and applies the gate. On return, the
        plan is approved (automatically by policy OR by a named human) and the
        workflow may provision/implement. Raises `_PlanRejected`/`_BlockNow`/
        `_EscalateNow`/`_CancelledByOperator` on the paths that do not proceed."""
        input = self._input

        await self._boundary_gate()
        await self._budget_boundary("planner")
        plan: PlanArtifact = await self._run_model_activity(
            ACTIVITY_RUN_PLANNER_TURN,
            {
                "work_item_id": input.work_item_id,
                "tenant_id": input.tenant_id,
                "repo": input.repo,
                "base_branch": input.base_branch,
                # S1: real instruction (issue content + acceptance + clarifications).
                "instruction": self._agent_instruction(),
                "model_override": self._model_override,
            },
            PlanArtifact,
        )
        input.plan_json = plan.model_dump()
        input.plan_rounds += 1
        await self._audit(
            "planner_completed",
            {"risk_class_declared": plan.risk_class, "expected_files": plan.expected_files,
             "round": input.plan_rounds},
        )

        # P1: EFFECTIVE risk classification by deterministic POLICY (outside the
        # model) — a Planner that under-classifies does NOT weaken the gate.
        effective_risk = policy.classify_risk(plan.expected_files, plan.risk_class)
        input.risk_class = effective_risk

        if workflow.patched("reject-empty-expected-files-v1"):
            if not plan.expected_files and not plan.no_code_change:
                await self._audit(
                    "planner_contract_rejected",
                    {"reason": "expected_files_empty", "round": input.plan_rounds},
                )
                raise _EscalateNow("planner_expected_files_empty_without_no_code_change")

        # Postgres receives plan/risk before the Coder. The hash and the
        # expected_files list are derived by the Activity server-side.
        if workflow.patched("persist-plan-before-coder-v1"):
            await self._persist_status()

        if not policy.requires_plan_approval(effective_risk, input.require_approval_risk_classes):
            # Low risk -> auto-approve BY POLICY (never because an approver is
            # missing). Project and audit.
            await self._record_gate(status="approved", auto_approved=True,
                                    approvers=[], decided_by=None)
            await self._audit("plan_auto_approved", {"risk_class": effective_risk})
            return

        # High risk -> resolve the approver via the CODEOWNERS -> access bundle cascade.
        resolved = await workflow.execute_activity(
            LOCAL_ACTIVITY_RESOLVE_APPROVER,
            {"tenant_id": input.tenant_id, "repo": input.repo,
             "work_item_id": input.work_item_id, "channel": None},
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )
        approvers = list(resolved.get("approvers") or [])
        input.approvers = approvers

        if not approvers:
            # EMPTY CASCADE -> Blocked + escalation, NEVER auto-approve (P1/P3).
            await self._record_gate(status="blocked", auto_approved=False,
                                    approvers=[], decided_by=None)
            await self._audit("plan_gate_no_approver_blocked", {"risk_class": effective_risk})
            raise _BlockNow(
                f"plan_approval_cascade_empty:no CODEOWNERS/designated approver for tenant={input.tenant_id}"
            )

        # Park on a DURABLE wait (new awaiting_plan_approval state).
        # ORDER (fixed during the Phase 3 integration): write the gate
        # projection BEFORE flipping the status to awaiting_plan_approval.
        # Invariant: the durable gate (consumed by the queue board and by WS-A's
        # signal routing, which fires SIGNAL_PLAN_APPROVAL based on the status)
        # must exist at the instant the state becomes observable — otherwise an
        # observer that sees the status may read a still-absent gate (a real
        # race reproduced by a test; the Phase 3 edit to the review loop changed
        # the timing and exposed the inversion).
        await self._record_gate(status="pending", auto_approved=False,
                                approvers=approvers, decided_by=None)
        await self._set_raw_status(
            STATUS_AWAITING_PLAN_APPROVAL,
            audit_action="awaiting_plan_approval",
            details={"risk_class": effective_risk, "approvers": approvers,
                     "source": resolved.get("source")},
        )
        # Render the approval request via the adapters (WS-A edits the single
        # status message in-place; here we only ask it to reflect the gate).
        try:
            await workflow.execute_activity(
                ACTIVITY_POST_TRACKING_COMMENT,
                {"work_item_id": input.work_item_id, "tenant_id": input.tenant_id,
                 "pr_number": None, "status": "awaiting_plan_approval"},
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
        except ActivityError:
            logger.warning("the gate's post_tracking_comment failed; the gate stays durable anyway")

        # Durable wait for SIGNAL_PLAN_APPROVAL (routed by WS-A when
        # status==awaiting_plan_approval). Anti-clobber: we do not reset the
        # flag before reading the payload.
        def gate_decided() -> bool:
            return (self._plan_approval_received or self._cancelled
                    or self._operator_escalate_requested)

        # REPLAY GUARD — placed here, immediately before the wait, because THIS
        # is the exact point where the command sequence forks: a
        # `wait_condition(timeout=...)` issues a StartTimer command that the
        # history of a run already parked at this gate does not contain, and
        # there are such runs in flight right now. Replay of those histories
        # finds no marker, takes the untimed branch, and reproduces the original
        # commands one-for-one; only new executions record the marker and get a
        # deadline. Everything the deadline triggers (reminder, gate projection,
        # escalation) hangs off the patched branch, so nothing below can inject
        # a command into an old history.
        if workflow.patched("plan-approval-timeout-v1"):
            timeout_hours = input.plan_approval_timeout_hours
            if timeout_hours <= 0:
                # Deadline explicitly disabled by the operator (see the field
                # docs in models.py) — same unbounded park as before.
                await workflow.wait_condition(gate_decided)
            else:
                reminder_delta, remaining_delta, reminder_overridden = _approval_windows(
                    input.plan_approval_reminder_hours, timeout_hours
                )
                if reminder_overridden:
                    logger.warning(
                        "plan approval reminder (%gh) does not fit inside the deadline (%gh); "
                        "reminding at %gh instead so the ping still precedes the escalation",
                        input.plan_approval_reminder_hours, timeout_hours,
                        reminder_delta.total_seconds() / 3600.0,
                    )
                decided = await self._wait_with_reminder(
                    condition=gate_decided,
                    reminder_delta=reminder_delta,
                    remaining_delta=remaining_delta,
                    reminder_action="plan_approval_reminder_sent",
                    on_reminder=self._post_plan_approval_reminder,
                )
                if not decided:
                    # Either raises _EscalateNow (no verdict: it neither
                    # self-approves nor fails silently) or returns, meaning a
                    # verdict landed after the timer fired and the normal verdict
                    # handling below owns it.
                    await self._expire_plan_approval(
                        approvers, effective_risk, gate_decided=gate_decided
                    )
        else:
            await workflow.wait_condition(gate_decided)

        if self._cancelled:
            raise _CancelledByOperator()
        if self._operator_escalate_requested:
            raise _EscalateNow(self._operator_escalate_reason or "operator_escalate")

        payload = self._plan_approval_payload or {}
        self._plan_approval_received = False  # consumed
        verdict = payload.get("verdict")
        actor = payload.get("actor") or payload.get("decided_by") or "unknown"

        if verdict == "approved":
            await self._record_gate(status="approved", auto_approved=False,
                                    approvers=approvers, decided_by=actor)
            await self._audit("plan_approved", {"decided_by": actor, "risk_class": effective_risk})
            input.status = WorkItemStatus.queued.value
            await self._persist_status()
            return

        if verdict == "rejected":
            route = payload.get("route")
            justification = payload.get("justification") or payload.get("comment") or ""
            # A rejection is ALWAYS audited with identity + justification (WSB-E3-T3).
            await self._record_gate(status="rejected", auto_approved=False, approvers=approvers,
                                    decided_by=actor, route=route, justification=justification)
            await self._audit(
                "plan_rejected",
                {"decided_by": actor, "route": route, "justification": justification,
                 "risk_class": effective_risk},
            )
            if route not in ("re_plan", "re_clarify", "cancel"):
                raise _EscalateNow(f"plan_rejected_unknown_route:{route!r}")
            raise _PlanRejected(route=route, actor=actor, justification=justification)

        raise _EscalateNow(f"unknown_plan_verdict:{verdict!r}")

    async def _post_plan_approval_reminder(self) -> None:
        """Rewrites the body of the ONE mutable gate message when the reminder
        window elapses, KEEPING the status at `awaiting_plan_approval`.

        The status is not cosmetic here. The first version of this reminder
        invented a pseudo-status (`awaiting_plan_approval_reminder`) and that
        DESTROYED the gate it was reminding about: adapter-slack attaches
        `approval_blocks(...)` only when `status == "awaiting_plan_approval"`
        (services/adapter-slack/adapter_slack/app.py, `/internal/status-comment`),
        and `SlackCommentBackend.edit` only forwards `blocks` when the caller
        supplies them — so the reminder made Slack `chat_update` the message with
        no Block Kit and the Approve/Reject buttons, the ONLY way a Slack approver
        can answer, were replaced by plain text. The reminder made the task
        unanswerable. Real status + `body` override (the shape
        `_post_preview_link` already uses) keeps the buttons and still changes
        what the human reads.

        HONEST LIMIT — THIS DOES NOT NOTIFY ANYBODY. Every outbound path the
        orchestrator has (`post_tracking_comment` -> each adapter's
        `/internal/status-comment` -> `MutableCommentWriter`) EDITS the single
        status message: Slack `chat_update` and GitHub's comment PATCH are both
        silent by design, mentions added by an edit included. So this reminder
        only pays off for a human who comes back to the thread, and the escalation
        at the deadline is equally silent. Making it ring needs a NEW message,
        which needs an endpoint no adapter has today: `POST /internal/nudge`
        posting a Slack thread reply (`chat_postMessage(thread_ts=...)`, which
        leaves the approval message and its buttons untouched), a GitHub
        `issues.create_comment`, a Jira `add_comment` — plus its own local
        activity here behind its own patch marker. That spans adapter-slack,
        adapter-github, adapter-jira and the contracts package, so it is not in
        this change; the deadline is deliberately long enough (72h) that a human
        returning to the thread sees the countdown before the item escalates.

        Best-effort: an adapter that is down must not consume the item's deadline
        or take the workflow down.
        """
        input = self._input
        _, remaining, _ = _approval_windows(
            input.plan_approval_reminder_hours, input.plan_approval_timeout_hours
        )
        remaining_hours = remaining.total_seconds() / 3600.0
        who = ", ".join(input.approvers) or "the resolved approvers"
        # The body has to repeat the ask, because on Slack it REPLACES the plan
        # text inside the same Block Kit message (the buttons sit under it).
        body = (
            "⏰ **Reminder — this plan is still waiting for a decision.**\n\n"
            f"📋 Plan ready — awaiting human approval (risk: {input.risk_class}).\n"
            f"Approvers: {who}.\n\n"
            f"Approve or reject it within ~{remaining_hours:.0f}h. After that the DSE "
            "stops and hands the task to a human owner (`escalated`) — it never "
            "approves a plan because nobody answered."
        )
        try:
            await workflow.execute_activity(
                ACTIVITY_POST_TRACKING_COMMENT,
                {"work_item_id": input.work_item_id, "tenant_id": input.tenant_id,
                 "status": STATUS_AWAITING_PLAN_APPROVAL, "body": body},
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
        except ActivityError:
            logger.warning(
                "best-effort plan approval reminder failed; the deadline still stands"
            )

    async def _expire_plan_approval(self, approvers: list[str], effective_risk: str,
                                    *, gate_decided) -> None:
        """The approval deadline elapsed. Raises `_EscalateNow` when there is
        really no verdict, or RETURNS when one landed after the timer fired (the
        caller then handles it as an ordinary decision).

        THE RE-CHECK IS THE POINT, not defensive noise. `wait_condition(timeout=)`
        reports the timeout even when the signal that answers the gate is
        delivered in the very workflow task that carries the fired timer, and
        further signals can arrive while this coroutine is scheduled. Without the
        re-check a human verdict that beat the deadline by milliseconds was
        silently DISCARDED and the append-only ledger ended up holding both
        "approval delivered" and "timed out, no verdict" for the same item
        (reproduced against the time-skipping test server). The re-check runs
        BEFORE the first write, so no timeout that did not happen ever reaches the
        ledger; a verdict arriving after this point loses the race honestly, with
        `escalated` — a state a human owns — as the outcome.

        No self-approval (absence of an answer is never consent — P1/P3) and no
        silent failure: the item lands on the `escalated` terminal state.

        The gate projection goes to 'blocked' rather than a new 'expired' because
        migration 0009 CHECK-constrains plan_approval_gate.status to
        pending/approved/rejected/blocked; the `justification` carries the
        distinction until that enum grows. ONE audit row, written at a real state
        transition — never one per timer tick (a stuck item once produced ~2,900
        rows that way)."""
        if gate_decided():
            await self._audit(
                "plan_approval_verdict_raced_deadline",
                {"risk_class": effective_risk, "approvers": approvers,
                 "timeout_hours": self._input.plan_approval_timeout_hours},
            )
            return

        hours = self._input.plan_approval_timeout_hours
        reason = f"plan_approval_timeout_after_{hours:g}h"
        reminder_delta, _, reminder_overridden = _approval_windows(
            self._input.plan_approval_reminder_hours, hours
        )
        await self._record_gate(
            status="blocked", auto_approved=False, approvers=approvers,
            decided_by=None,
            justification=(f"{reason}: no verdict from "
                           f"{', '.join(approvers) or 'no resolvable approver'}"),
        )
        await self._audit(
            "plan_approval_timed_out",
            {"risk_class": effective_risk, "approvers": approvers,
             "timeout_hours": hours,
             "reminder_hours": self._input.plan_approval_reminder_hours,
             # When the two knobs contradict each other the reminder is moved
             # (see `_approval_windows`); the row records what actually happened,
             # not what was configured.
             "reminder_hours_effective": reminder_delta.total_seconds() / 3600.0,
             "reminder_hours_overridden": reminder_overridden},
        )
        raise _EscalateNow(reason)

    async def _record_gate(self, *, status: str, auto_approved: bool, approvers: list[str],
                           decided_by: str | None, route: str | None = None,
                           justification: str | None = None) -> None:
        """Durable projection of the gate (migration 0009) — queryable by the
        queue board/operators. Does NOT replace the audit ledger (the calling
        method also calls `_audit`)."""
        await workflow.execute_activity(
            LOCAL_ACTIVITY_RECORD_GATE,
            {
                "work_item_id": self._input.work_item_id,
                "tenant_id": self._input.tenant_id,
                "risk_class": self._input.risk_class,
                "status": status,
                "auto_approved": auto_approved,
                "resolved_approvers": approvers,
                "decided_by": decided_by,
                "rejection_route": route,
                "justification": justification,
                "plan_round": self._input.plan_rounds,
            },
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )

    async def _run_l2_review(self, coder_result: CoderTurnResult) -> "L2Verdict":
        """WSC-E3-T5/WSE-E2 — fresh-context Reviewer L2 session. P3
        (NON-NEGOTIABLE): the payload contains ONLY the plan artifact + the
        final diff; NOTHING from the Coder's history/instructions (no
        `instructions`, `clarification_notes`, `objections` — deliberately
        absent)."""
        input = self._input
        payload = {
            "work_item_id": input.work_item_id,
            "tenant_id": input.tenant_id,
            "plan": input.plan_json,
            "diff": coder_result.diff_summary,
        }
        if workflow.patched("sha-bound-l2-input-v1"):
            payload.update({"base_sha": input.base_sha, "head_sha": input.head_sha})
        l2: L2Verdict = await self._run_model_activity(
            ACTIVITY_RUN_L2_REVIEW,
            payload,
            L2Verdict,
        )
        await self._audit("l2_review_completed",
                          {"passed": l2.passed, "status": l2.status.value,
                           "objections": l2.objections,
                           "base_sha": input.base_sha, "head_sha": input.head_sha})
        return l2

    # ------------------------------------------------------------------
    # WSB-E3-T3 — rejection path: 3 deterministic routes. None of them starts
    # implementation without going through the corresponding gate again.
    # ------------------------------------------------------------------
    async def _handle_plan_rejection(self, exc: "_PlanRejected") -> WorkItemLifecycleResult:
        input = self._input
        if exc.route == "cancel":
            detail = f"plan_rejected_cancel: {exc.justification}"
            input.terminal_detail = detail
            await self._set_status(
                WorkItemStatus.failed, audit_action="plan_rejected_cancelled",
                details={"decided_by": exc.actor, "justification": exc.justification},
            )
            return WorkItemLifecycleResult(
                work_item_id=input.work_item_id, status=WorkItemStatus.failed.value,
                detail=detail, pr_number=input.pr_number,
            )

        if exc.route == "re_clarify":
            # Back to the CLARIFICATION gate (not to implementation): re-enters
            # intake from scratch — implementation only restarts after
            # clarification + planner + gate again. Reopening clarification
            # means requiring the requester to re-supply the acceptance criteria
            # (the approver rejected precisely because the scope was ambiguous):
            # we clear `acceptance_criteria` so the deterministic checklist
            # reopens the round (it never "guesses" — P1/P6).
            input.phase = PHASE_INTAKE
            input.status = WorkItemStatus.needs_clarification.value
            input.clarification_rounds = 0
            input.acceptance_criteria = None
            input.plan_json = {}
            input.risk_class = "low"
            input.approvers = []
            await self._audit("plan_rejected_route_re_clarify", {"decided_by": exc.actor})
            await self._persist_status()
            return await self._continue_as_new(input)

        if exc.route == "re_plan":
            # Re-plan: clears the plan and re-enters the implementation phase,
            # which starts from the Planner + gate again (it never skips the
            # gate). Capped.
            if input.plan_rounds >= input.plan_round_cap:
                return await self._finish_escalated(
                    f"plan_round_cap_exhausted:{input.plan_rounds}"
                )
            input.plan_json = {}
            input.approvers = []
            input.status = WorkItemStatus.queued.value
            await self._audit("plan_rejected_route_re_plan",
                              {"decided_by": exc.actor, "round": input.plan_rounds})
            await self._persist_status()
            return await self._run_implementation_phase()

        return await self._finish_escalated(f"plan_rejected_unknown_route:{exc.route}")

    # ------------------------------------------------------------------
    # Phase 3 — human review (WSB-E3-T4). NO path calls the GitHub API's merge
    # here — it only waits for `merged_by_human` (P3: no agent session
    # approves/merges its own work).
    # ------------------------------------------------------------------
    def _drain_review_comments(self) -> list[dict[str, Any]]:
        """Consumes the ENTIRE batch of pending comments. Anti-clobber: it is
        only called AFTER `_review_received` becomes True; the signal handler
        merely appends to the list, so nothing is lost between the drain and the
        next wait."""
        comments = list(self._review_comments)
        self._review_comments.clear()
        self._review_received = False
        return comments

    def _bump_review_round(self) -> None:
        """WSB-E4-T2 — explicit cap on review rounds (the review loop was the
        only `while` in the workflow that still lacked its own cap). Exhausted
        -> escalated (never an infinite loop, by construction)."""
        self._input.review_round += 1
        if self._input.review_round > self._input.review_round_cap:
            raise _EscalateNow(
                f"review_round_cap_exhausted:{self._input.review_round - 1}"
            )

    async def _update_base_branch_before_review_fix(self) -> None:
        """Phase 4 (WSE-E6-T16, WS-B wiring) — on the changes_requested path,
        BEFORE re-running the Coder: updates the task branch with the base drift
        WITHOUT rewriting history.

        `first_human_review_done=True` here is non-negotiable: we reach this
        point precisely BECAUSE a human already reviewed the PR and requested
        changes — after the 1st review the only allowed strategy is
        merge-base-into-branch. A rebase+force-push would orphan the review
        threads anchored to the rewritten commits (verified GitHub behavior,
        failure mode 11). The strategy is deterministic (P1: code in WS-E, not a
        model). An unresolvable conflict -> escalate to a human (NEVER force a
        resolution)."""
        input = self._input
        if not (input.repo and input.branch and input.base_branch):
            # Strict mode / no known branch: nothing to merge. Decline cleanly.
            await self._audit("base_branch_update_skipped_no_branch", {})
            return
        await self._boundary_gate()
        result: UpdateBaseBranchResult = await workflow.execute_activity(
            ACTIVITY_UPDATE_BASE_BRANCH,
            {
                "work_item_id": input.work_item_id,
                "tenant_id": input.tenant_id,
                "repo": input.repo,
                "branch": input.branch,
                "base_branch": input.base_branch,
                "first_human_review_done": True,
            },
            result_type=UpdateBaseBranchResult,
            **self._activity_timeouts(),
        )
        await self._audit(
            "base_branch_updated",
            {"strategy": result.strategy, "conflict": result.conflict,
             "orphaned_threads": result.orphaned_threads, "detail": result.detail},
        )
        if result.conflict:
            # P6: never force a resolution; escalate with the drift's identity.
            raise _EscalateNow(
                f"base_branch_merge_conflict:repo={input.repo} branch={input.branch} "
                f"base={input.base_branch} ({result.detail})"
            )
        if result.orphaned_threads:
            # Phase 4 exit invariant: merge-base NEVER orphans threads
            # (post-review rebase is forbidden by construction). If the owner
            # (WS-E) reported >0, something violated the guarantee — we do not
            # keep guessing (P6).
            raise _EscalateNow(
                f"base_branch_orphaned_threads:{result.orphaned_threads} "
                f"(strategy={result.strategy}) — violates the zero-orphaned-threads invariant"
            )

    async def _emit_pr_quality_metric(self, outcome: str) -> None:
        """Phase 4 — emits the PR quality metrics (pilot gate "PR quality
        thresholds"). Deterministic read in the workflow (counters + time via
        workflow.now); OTel emission in the local Activity. Best-effort: a
        metric failure never affects the flow. Called at the PR's terminal
        boundaries (merge/escalation)."""
        input = self._input
        time_to_merge = None
        if outcome == "merged" and input.pr_finalized_at_epoch is not None:
            time_to_merge = max(0.0, workflow.now().timestamp() - input.pr_finalized_at_epoch)
        payload = {
            "work_item_id": input.work_item_id,
            "tenant_id": input.tenant_id,
            "outcome": outcome,
            "review_rounds": input.review_round,
            "changes_requested_count": input.changes_requested_count,
            "evidence_refreshes": input.evidence_refreshes,
            "time_to_merge_seconds": time_to_merge,
        }
        try:
            await workflow.execute_activity(
                LOCAL_ACTIVITY_EMIT_PR_QUALITY_METRIC,
                payload,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError:
            logger.warning("emit_pr_quality_metric failed; the metric is best-effort, the flow continues")

    def _validate_merge_signal(self) -> tuple[bool, str, MergedByHumanSignal | None]:
        """Validates the envelope already authenticated/correlated by the adapter.

        Remotely verifying the PR state still belongs to the adapter/finalizer;
        here we prevent an empty signal, one from another PR/repo/SHA, or one
        without a human identity, from completing the workflow.
        """
        try:
            merge = MergedByHumanSignal(**(self._merge_payload or {}))
        except Exception as exc:  # a signal payload is untrusted data
            return False, f"invalid_payload:{type(exc).__name__}", None
        if self._input.pr_number is None or merge.pr_number != self._input.pr_number:
            return False, "pr_number_mismatch", merge
        if merge.repo and self._input.repo and merge.repo != self._input.repo:
            return False, "repo_mismatch", merge
        if merge.head_sha and self._input.head_sha and merge.head_sha != self._input.head_sha:
            return False, "head_sha_mismatch", merge
        return True, "ok", merge

    async def _verify_merge_via_github_api(self, merge) -> tuple[bool, str]:
        """Plan 08 §F (F1) — confirms the merge against the GitHub API (the
        truth), not just the envelope. Returns (ok_to_complete, reason).

        Policy:
          - PR actually merged (and head_sha matches) → (True, "api_verified").
          - DEFINITIVE refutation (PR does not exist / is not merged / sha
            diverges) → (False, reason): strong forgery signal; do NOT complete (P1).
          - API unavailable (network/credential error) → (True, "api_unavailable"):
            degrade to the envelope, which already passed the webhook's HMAC
            signature + correlation (defense in depth; do not stall a merge on
            an outage).
        Replay guard: old histories did not call this Activity."""
        if not workflow.patched("merge-verify-github-api-v1"):
            return True, "unpatched"
        if not self._input.pr_number or not self._input.repo:
            return True, "no_target"
        try:
            v: MergeVerification = await workflow.execute_activity(
                ACTIVITY_VERIFY_MERGE_STATE,
                {"work_item_id": self._input.work_item_id, "tenant_id": self._input.tenant_id,
                 "repo": self._input.repo, "pr_number": self._input.pr_number,
                 "expected_head_sha": self._input.head_sha},
                result_type=MergeVerification,
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError as exc:
            # The whole Activity failed after retries: degrade to the
            # authenticated envelope (do not stall a legitimate merge on an outage).
            logger.warning("verify_merge_state unavailable; degrading to the envelope: %s", exc)
            return True, "api_unavailable_activity_error"
        if v.verified:
            return True, f"api_verified(merged_by={v.merged_by})"
        # definitive refutation vs unavailability (distinct fail-safe)
        if v.reason.startswith(("not_merged", "pr_not_found", "head_sha_mismatch")):
            return False, v.reason
        return True, f"api_unavailable:{v.reason}"

    async def _run_review_phase(self) -> WorkItemLifecycleResult:
        """`while True` loop (NOT `continue_as_new` per iteration — see the
        comment in `_run_implementation_phase` about the signal race): each pass
        re-queries CI, waits for the human verdict, and if it is
        `changes_requested` (or CI red) applies the fix cycle on the SAME
        branch/PR and goes back to the top, all in the SAME workflow execution.
        Phase 3 (WSB-E4-T2): rounds capped by `review_round_cap`; comments
        consumed in a BATCH with a debounce window (ADR-26) — N comments in one
        window become ONE fix cycle + ONE evidence refresh."""
        input = self._input

        while True:
            await self._boundary_gate()
            # Feeds the history alert: the review loop is where the history
            # grows without Continue-As-New (documented limitation) — emit the
            # metric on every pass to the collector/WS-F.
            await self._emit_history_metric("review_loop")
            fine_states = workflow.patched("ci-pending-blocks-review-v1")
            # REPLAY GUARD — the wall-clock branch below issues an Activity
            # command at a point where in-flight histories recorded only a
            # StartTimer. On replay this marker is absent, the deadline branch is
            # skipped, and the old command sequence is reproduced one for one;
            # only live execution records it.
            ci_deadline = fine_states and workflow.patched("ci-wait-deadline-v1")
            if fine_states:
                input.ci_status = "pending"
                await self._set_status(
                    WorkItemStatus.ci_pending,
                    audit_action="ci_pending",
                    details={"head_sha": input.head_sha},
                )
            ci_payload = {
                "work_item_id": input.work_item_id,
                "tenant_id": input.tenant_id,
                "repo": input.repo,
                "ref": (input.head_sha or input.branch or "") if fine_states else (input.branch or ""),
                "pr_number": input.pr_number,
            }
            if fine_states:
                ci_payload.update({
                    "base_sha": input.base_sha or "",
                    "head_sha": input.head_sha or "",
                })
            try:
                ci: CiStatusResult = await workflow.execute_activity(
                    ACTIVITY_CONSUME_CI_STATUS,
                    ci_payload,
                    result_type=CiStatusResult,
                    retry_policy=RetryPolicy(maximum_attempts=max(1, input.activity_retry_cap)),
                    **self._activity_timeouts(),
                )
            except ActivityError as exc:
                raise _ActivityRetriesExhausted(
                    ACTIVITY_CONSUME_CI_STATUS, str(exc.cause or exc)[:300]
                )
            input.ci_status = ci.status
            await self._audit("ci_status_observed", {"status": ci.status})

            if fine_states and ci.status == "pending":
                input.ci_pending_polls += 1
                # NOT gated on the patch: assigning a dataclass field emits no
                # Temporal command, so it is replay-safe unconditionally — and
                # gating it would give every item already stuck for days a FRESH
                # clock at upgrade instead of one reconstructed from its own
                # recorded history.
                if input.ci_wait_started_at_epoch is None:
                    input.ci_wait_started_at_epoch = workflow.now().timestamp()
                await self._persist_status()
                if input.ci_pending_polls >= input.ci_pending_poll_cap:
                    if ci_deadline:
                        await self._exhaust_ci_wait("poll_cap")
                    raise _EscalateNow(
                        f"ci_pending_poll_cap_exhausted:{input.ci_pending_polls}"
                    )
                if ci_deadline and input.ci_wait_deadline_hours > 0:
                    elapsed = workflow.now().timestamp() - input.ci_wait_started_at_epoch
                    if elapsed >= input.ci_wait_deadline_hours * 3600:
                        await self._exhaust_ci_wait("wall_clock", elapsed_seconds=elapsed)
                        raise _EscalateNow(
                            f"ci_wait_deadline_exhausted:elapsed_s={int(elapsed)}"
                        )
                await workflow.sleep(timedelta(seconds=max(0.01, input.ci_poll_interval_seconds)))
                continue

            if ci.status == "red":
                # CI red before waking a human: back to the Coder on the SAME
                # branch/PR (same retry-cap discipline).
                input.coder_retry_count += 1
                if input.coder_retry_count > self._input.coder_retry_cap:
                    raise _EscalateNow("ci_red_after_retry_cap_exhausted")
                self._bump_review_round()
                await self._set_status(WorkItemStatus.review_feedback, audit_action="ci_red_retrying")
                fix_result = await self._apply_coder_fix_cycle(["ci red: fix the pipeline"])
                input.ci_pending_polls = 0
                # A new head_sha starts a NEW wait; without this reset the fix
                # cycle inherits the previous commit's elapsed time and blows the
                # deadline on its first pending poll.
                input.ci_wait_started_at_epoch = None
                # ADR-26: fix cycle = new commit that changes behavior -> 1 refresh
                input.last_files_changed = list(fix_result.files_changed)
                await self._run_evidence_pipeline(fix_result.files_changed,
                                                  reason="fix_cycle_ci_red")
                continue

            if ci.status == "no_ci":
                # The repo reported NOTHING — no check run and no commit status
                # — which is a terminal observation, not a wait. No patch marker
                # is needed: `no_ci` is unreachable in any pre-upgrade history
                # because the Activity could not produce it, and the workflow and
                # the Activity ship in the same image.
                #
                # It proceeds to human review rather than escalating. Escalation
                # is terminal, so escalating here would convert "0 of 25 finish"
                # into "25 of 25 escalate" while throwing away a PR and evidence
                # that already exist. P3 is not weakened: the merge gate is a
                # HUMAN signal verified against the GitHub API, and CI was never
                # the gate P3 depends on — it is an extra gate this repo does not
                # have. The comment below makes that explicit to the reviewer
                # rather than letting a silent green imply verification.
                await self._audit("ci_no_ci_detected", {
                    "reason": "no_ci_configured",
                    "status": ci.status,
                    "pr_number": input.pr_number,
                    "head_sha": input.head_sha,
                })
                # Body override on the real status, the shape the plan-approval
                # reminder already uses: the reviewer must not read the ordinary
                # "ready for human review" line and assume something verified
                # this commit.
                try:
                    await workflow.execute_activity(
                        ACTIVITY_POST_TRACKING_COMMENT,
                        {"work_item_id": input.work_item_id, "tenant_id": input.tenant_id,
                         "status": "pr_ready",
                         "body": (
                             "✅ PR opened with the change and evidence — ready for human "
                             "review.\n\n"
                             "⚠️ **This repository has no CI**: no check run and no commit "
                             "status was reported for this commit. Nothing automated verified "
                             "this change beyond the DSE's own L1/L2 gates. You are the only "
                             "gate."
                         )},
                        start_to_close_timeout=timedelta(seconds=60),
                        retry_policy=RetryPolicy(maximum_attempts=5),
                    )
                except ActivityError:
                    logger.warning("best-effort no_ci notice failed; the item still proceeds")

            if fine_states and ci.status not in ("green", "no_ci"):
                raise _EscalateNow(f"unknown_ci_status:{ci.status!r}")

            # IMPORTANT: we do not reset `_review_received` here before
            # waiting — a `review_comment` may already have arrived (via the
            # signal callback, which runs as soon as the workflow event loop
            # yields) while we were still in the CI Activity above. Resetting
            # here would erase that legitimate answer and the workflow would
            # wait forever for a signal that already came. We only consume and
            # reset AFTER draining the batch, below.
            ready_status = WorkItemStatus.review_ready if fine_states else WorkItemStatus.pr_ready
            await self._set_status(ready_status, audit_action="awaiting_human_review",
                                   details={"ci_status": ci.status, "head_sha": input.head_sha})
            await workflow.wait_condition(
                lambda: self._review_received or self._refresh_evidence_requested
                or self._cancelled or self._operator_escalate_requested
            )
            if self._cancelled:
                raise _CancelledByOperator()
            if self._operator_escalate_requested:
                raise _EscalateNow(self._operator_escalate_reason or "operator_escalate")

            if self._refresh_evidence_requested and not self._review_received:
                # ADR-26 — explicit human request: the ONLY refresh trigger
                # without a new commit. With no new files, reuse the last known
                # set for the deterministic paths-filter (FR-20). The request is
                # consumed HERE (not only inside the pipeline): a refresh
                # DECLINED by the cap also counts as served — otherwise the
                # pending flag would turn this wait into a hot loop.
                self._refresh_evidence_requested = False
                await self._audit("evidence_refresh_requested_by_human", {})
                await self._run_evidence_pipeline(list(input.last_files_changed),
                                                  reason="human_request")
                continue

            comments = self._drain_review_comments()

            # ADR-26 — debounce: if the batch requests changes and a window is
            # configured, wait out the window to group comments that are still
            # arriving — 6 comments in one window = 1 fix + 1 refresh. 100%
            # deterministic decision: counting/comparison + Temporal's durable
            # timer; no LLM decides anything here (P1).
            if (input.evidence_debounce_seconds > 0
                    and any(c.get("verdict") == "changes_requested" for c in comments)):
                try:
                    await workflow.wait_condition(
                        lambda: self._cancelled or self._operator_escalate_requested,
                        timeout=timedelta(seconds=input.evidence_debounce_seconds),
                    )
                except asyncio.TimeoutError:
                    pass
                if self._cancelled:
                    raise _CancelledByOperator()
                if self._operator_escalate_requested:
                    raise _EscalateNow(self._operator_escalate_reason or "operator_escalate")
                late = self._drain_review_comments()
                comments.extend(late)
                await self._audit(
                    "review_comments_debounced",
                    {"count": len(comments), "window_s": input.evidence_debounce_seconds},
                )

            verdicts = [c.get("verdict") for c in comments]

            if "changes_requested" in verdicts:
                self._bump_review_round()
                input.changes_requested_count += 1
                texts = [str(c.get("comment") or "") for c in comments
                         if c.get("verdict") == "changes_requested"]
                await self._set_status(WorkItemStatus.review_feedback, audit_action="changes_requested",
                                        details={"comments": texts, "batched": len(comments)})
                # Phase 4 (WSE-E6-T16) — merge-base BEFORE re-running the Coder:
                # the human already reviewed (first_human_review_done=True), so
                # the base drift comes in via merge-base-into-branch, never a
                # rebase (preserves the review threads). Conflict -> escalate.
                await self._update_base_branch_before_review_fix()
                fix_result = await self._apply_coder_fix_cycle(texts)
                # ADR-26: 1 batch of comments -> 1 fix cycle -> at most 1
                # evidence refresh (the refresh is triggered by the fix COMMIT,
                # never by the comments themselves).
                input.last_files_changed = list(fix_result.files_changed)
                await self._run_evidence_pipeline(fix_result.files_changed, reason="fix_cycle")
                continue

            if "approved" in verdicts:
                merge_status = (
                    WorkItemStatus.merge_pending if fine_states else WorkItemStatus.pr_ready
                )
                await self._set_status(merge_status, audit_action="approved_awaiting_merge")
                # (no reset of `_merged` here for the same reason as above — it
                # starts False in __init__ and only this branch consumes it,
                # once per run)
                require_verified_signal = workflow.patched("validate-merge-signal-v1")
                while True:
                    await workflow.wait_condition(lambda: self._merged or self._cancelled)
                    if self._cancelled:
                        raise _CancelledByOperator()
                    if not require_verified_signal:
                        break
                    valid, reason, merge = self._validate_merge_signal()
                    if valid:
                        # Plan 08 §F (F1): the envelope (pr_number/repo/sha) is
                        # not a secret — a forged webhook with the right fields
                        # would pass the check above. Confirm on the GitHub API
                        # that the PR is REALLY merged before completing (P1/P8).
                        api_ok, api_reason = await self._verify_merge_via_github_api(merge)
                        if api_ok:
                            await self._audit(
                                "merge_signal_verified",
                                {"merged_by": merge.merged_by,
                                 "pr_number": merge.pr_number,
                                 "merge_sha": merge.merge_sha,
                                 "head_sha": input.head_sha,
                                 "api_verification": api_reason},
                            )
                            break
                        reason = f"api_refuted:{api_reason}"
                    await self._audit(
                        "merge_signal_rejected",
                        {"reason": reason, "payload": dict(self._merge_payload or {})},
                    )
                    self._merged = False
                    self._merge_payload = None
                await self._checkpoint_or_rebuild("done")
                if input.sandbox_id:
                    try:
                        await workflow.execute_activity(
                            ACTIVITY_TEARDOWN_SANDBOX,
                            {"work_item_id": input.work_item_id, "tenant_id": input.tenant_id,
                             "stage": "done"},
                            start_to_close_timeout=timedelta(seconds=120),
                            retry_policy=RetryPolicy(maximum_attempts=3),
                        )
                    except ActivityError:
                        logger.warning("teardown failed after merge; continuing anyway (it does not block Done)")
                await self._set_status(
                    WorkItemStatus.done,
                    audit_action="merged_by_human",
                    details={"merged_by": (self._merge_payload or {}).get("merged_by"),
                             "pr_number": input.pr_number},
                )
                # Human merge -> Done column on the board (closes the card's cycle).
                await self._post_board_transition(WorkItemStatus.done.value)
                # The merge is the one transition every requester actually waits
                # for, and it was the only consequential one that never wrote
                # back: the "done" copy existed in _STATUS_BODIES but no call
                # site ever emitted it, so the status comment froze on the last
                # pre-merge state and the task looked unfinished forever.
                if workflow.patched("post-done-comment-v1"):
                    await self._post_status_comment(WorkItemStatus.done.value)
                # Phase 4 — PR quality metric at the merge boundary (pilot
                # gate). Best-effort, after Done is already persisted.
                await self._emit_pr_quality_metric("merged")
                return WorkItemLifecycleResult(
                    work_item_id=input.work_item_id, status=WorkItemStatus.done.value,
                    pr_number=input.pr_number,
                )

            # unknown verdict: never guess (P6) — escalate.
            raise _EscalateNow(f"unknown_review_verdict:{verdicts!r}")

    async def _apply_coder_fix_cycle(self, comments: list[str]) -> CoderTurnResult:
        """`changes_requested` (human, possibly a debounced BATCH) or CI red:
        back to the Coder on the SAME branch/PR, re-validate L1, re-finalize the
        SAME PR (idempotent). Returns the Coder result — the caller uses
        `files_changed` for the evidence refresh (paths-filter FR-20)."""
        input = self._input
        await self._boundary_gate()
        await self._budget_boundary("review_fix_coder")
        await self._maybe_retry_from_checkpoint()

        review_notes = "\n".join(f"- {c.strip()}" for c in comments if c and c.strip())
        fix_instruction = self._agent_instruction(include_objections=True)
        if review_notes:
            fix_instruction += "\n\nReview comments to resolve:\n" + review_notes
        coder_result: CoderTurnResult = await self._run_model_activity(
            ACTIVITY_RUN_CODER_TURN,
            {
                # S7: RunCoderTurnInput uses `instruction` (str) + `branch` — the
                # old call site sent `instructions` (a list, a nonexistent field).
                "sandbox_id": input.sandbox_id,
                "work_item_id": input.work_item_id,
                "tenant_id": input.tenant_id,
                "branch": input.branch,
                "instruction": fix_instruction,
                "model_override": self._model_override,
                "runtime_override": self._runtime_override,
                "expected_files": list((input.plan_json or {}).get("expected_files", [])),
            },
            CoderTurnResult,
        )
        await self._consume_cost(coder_result.cost_usd, source="coder_fix")
        await self._audit("coder_fix_applied", {"files_changed": coder_result.files_changed})

        await self._boundary_gate()
        await self._checkpoint_or_rebuild("review_feedback")

        await self._set_status(WorkItemStatus.validating, audit_action="l1_revalidation_started")
        await self._budget_boundary("review_fix_l1")
        l1_payload = {
            "sandbox": self._input.sandbox_handle,
            "plan": input.plan_json,
            "tenant_id": input.tenant_id,
            "base_branch": input.base_branch or "main",
        }
        if workflow.patched("sha-bound-review-l1-input-v1"):
            l1_payload.update({
                "work_item_id": input.work_item_id,
                "base_sha": input.base_sha or "",
                "head_sha": input.head_sha or "",
            })
        l1_result: L1Result = await self._run_model_activity(
            ACTIVITY_RUN_L1_PIPELINE, l1_payload, L1Result,
        )
        l1_passed = (
            l1_result.status == GateStatus.PASS
            if workflow.patched("structured-review-l1-status-v1")
            else l1_result.passed
        )
        if not l1_passed:
            input.coder_retry_count += 1
            if input.coder_retry_count > self._input.coder_retry_cap:
                raise _EscalateNow("l1_revalidation_failed_after_retry_cap")
            # try again recursively up to the cap (stays on the same branch/PR)
            return await self._apply_coder_fix_cycle(comments)

        await self._boundary_gate()
        finalize_payload = {
            "work_item_id": input.work_item_id,
            "tenant_id": input.tenant_id,
            "sandbox": self._input.sandbox_handle,
            "repo": input.repo,
            "base_branch": input.base_branch or "main",
            "branch": input.branch,
            "summary": self._pr_summary(),
            "issue_ref": ({"issue_number": input.issue_number}
                          if input.issue_number else None),
            "evidence_url": self._l1_evidence(l1_result),
        }
        if workflow.patched("sha-bound-refinalize-input-v1"):
            finalize_payload.update({
                "base_sha": input.base_sha or "",
                "head_sha": input.head_sha or "",
                "risk_class": input.risk_class,
            })
        try:
            pr_ref: PrRef = await workflow.execute_activity(
                ACTIVITY_FINALIZE_PR,
                finalize_payload,
                result_type=PrRef,
                retry_policy=RetryPolicy(maximum_attempts=max(1, input.activity_retry_cap)),
                **self._activity_timeouts(),
            )
        except ActivityError as exc:
            raise _ActivityRetriesExhausted(
                ACTIVITY_FINALIZE_PR, str(exc.cause or exc)[:300]
            )
        input.pr_number = pr_ref.pr_number
        input.pr_url = pr_ref.url
        input.head_sha = pr_ref.head_sha or input.head_sha
        await workflow.execute_activity(
            ACTIVITY_POST_TRACKING_COMMENT,
            {
                "work_item_id": input.work_item_id,
                "tenant_id": input.tenant_id,
                "pr_number": pr_ref.pr_number,
                "status": "pr_updated",
            },
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )
        input.ci_status = None
        # A refinalize means a new head_sha, so the CI wait starts over on both
        # axes. The count was already not reset here — an asymmetry with the
        # CI-red fix cycle that only becomes visible once the counter is paired
        # with a clock that IS reset there.
        input.ci_pending_polls = 0
        input.ci_wait_started_at_epoch = None
        refinalized_status = (
            WorkItemStatus.pr_open
            if workflow.patched("refinalize-resets-ci-v1")
            else WorkItemStatus.pr_ready
        )
        await self._set_status(
            refinalized_status,
            audit_action="pr_refinalized",
            details={"pr_number": pr_ref.pr_number, "head_sha": input.head_sha},
            clear_ci_status=True,
        )
        return coder_result

    # ------------------------------------------------------------------
    # Phase 3 — evidence pipeline (WS-E implements the Activities; here only the
    # deterministic orchestration, via the NAMES/models from dse_contracts).
    # trigger_preview -> (created) run_demo_evidence -> run_visual_diff.
    # Publishing the video/trace happens INSIDE run_demo_evidence (WS-E).
    # Failure mode 9: a degraded preview/demo NEVER blocks the PR — degraded
    # evidence is recorded (audit + projection 0014) and the flow moves on to
    # human review.
    # ------------------------------------------------------------------
    async def _run_evidence_pipeline(self, files_changed: list[str], *, reason: str) -> None:
        input = self._input
        if input.pr_number is None:
            # Strict mode (compare_url without a PR): without a pr_number there
            # is no per-PR preview — decline cleanly and audited (P6), never block.
            await self._audit("evidence_skipped_no_pr", {"reason": reason})
            return
        if reason != "initial":
            if input.evidence_refreshes >= input.evidence_refresh_cap:
                # ADR-26: refresh cap — decline CLEANLY (audited); the evidence
                # goes stale, the PR is not blocked (P6).
                await self._audit(
                    "evidence_refresh_declined_cap",
                    {"reason": reason, "refreshes": input.evidence_refreshes,
                     "cap": input.evidence_refresh_cap},
                )
                return
            input.evidence_refreshes += 1
        # Any pending human request is served by THIS refresh (it never queues a
        # second refresh for the same branch state).
        self._refresh_evidence_requested = False

        # Plan 08 §D — deploys_preview gate (operator-set in the Repos & ROI panel).
        # Replay guard: old histories did not call this local activity and must
        # replay with preview_enabled=True (previous behavior).
        preview_enabled = True
        if workflow.patched("preview-gate-deploys-v1"):
            try:
                gate = await workflow.execute_activity(
                    LOCAL_ACTIVITY_PREVIEW_ENABLED,
                    {"tenant_id": input.tenant_id, "repo": input.repo},
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
                preview_enabled = bool(gate.get("enabled", True))
            except ActivityError:
                logger.warning("preview_enabled_for_repo failed; fail-open (preview never blocks)")

        try:
            preview: PreviewRef = await workflow.execute_activity(
                ACTIVITY_TRIGGER_PREVIEW,
                {
                    "work_item_id": input.work_item_id,
                    "tenant_id": input.tenant_id,
                    "repo": input.repo,
                    "pr_number": input.pr_number,
                    "files_changed": list(files_changed),
                    "preview_enabled": preview_enabled,
                },
                result_type=PreviewRef,
                start_to_close_timeout=timedelta(seconds=900),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError as exc:
            input.preview_status = "degraded"
            await self._audit(
                "evidence_degraded",
                {"stage": "trigger_preview", "reason": reason,
                 "error": str(exc.cause or exc)[:300]},
            )
            await self._record_evidence(reason=reason, detail="trigger_preview_failed")
            return

        input.preview_status = preview.status
        input.preview_url = preview.url
        await self._audit(
            "preview_triggered",
            {"status": preview.status, "url": preview.url,
             "namespace": preview.namespace, "reason": reason},
        )

        if preview.status in ("skipped_backend_only", "skipped_disabled"):
            # Deterministic no-preview decision (paths-filter FR-20 or the
            # deploys_preview gate §D) — counts as SUCCESS, never blocks.
            await self._audit(
                "evidence_skipped_backend_only",
                {"reason": reason, "preview_status": preview.status},
            )
            await self._record_evidence(reason=reason, detail=preview.status)
            return
        if preview.status != "created":
            # "degraded" or any unknown status: degrade cleanly (P6), never
            # block and never guess.
            await self._audit(
                "evidence_degraded",
                {"stage": "trigger_preview", "reason": reason,
                 "status": preview.status, "detail": (preview.detail or "")[:300]},
            )
            await self._record_evidence(reason=reason, detail=(preview.detail or "")[:300])
            return

        # Plan 08 §D (D1) — the user's goal: the preview LINK shows up on the
        # PR for the human to open and decide. Posted as soon as the preview
        # exists (independent of demo/visual, which degrade without blocking).
        # Best-effort and patch-guarded (old histories did not post) — never blocks.
        if preview.url and workflow.patched("preview-link-in-pr-v1"):
            await self._post_preview_link(preview.url, preview.kind)

        try:
            demo: DemoEvidenceResult = await workflow.execute_activity(
                ACTIVITY_RUN_DEMO_EVIDENCE,
                {
                    "work_item_id": input.work_item_id,
                    "tenant_id": input.tenant_id,
                    "base_url": preview.url,
                },
                result_type=DemoEvidenceResult,
                start_to_close_timeout=timedelta(seconds=900),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError as exc:
            await self._audit(
                "evidence_degraded",
                {"stage": "run_demo_evidence", "reason": reason,
                 "error": str(exc.cause or exc)[:300]},
            )
            await self._record_evidence(reason=reason, detail="demo_evidence_failed")
            return

        input.evidence_passed = demo.passed
        input.evidence_video_key = demo.video_artifact_key
        input.evidence_trace_key = demo.trace_artifact_key
        await self._audit(
            "demo_evidence_completed",
            {"passed": demo.passed, "video_artifact_key": demo.video_artifact_key,
             "trace_artifact_key": demo.trace_artifact_key,
             "duration_s": demo.duration_s, "reason": reason},
        )

        # Visual diff when there is a screenshot. Contract gap documented in the
        # README: DemoEvidenceResult does not carry a screenshot key, so the
        # deterministic trigger is "the demo produced media" (video/trace) and
        # the candidate follows the demos/<work_item_id>/ convention (ADR-27,
        # WSC-E3-T4b).
        if demo.video_artifact_key or demo.trace_artifact_key:
            try:
                vd: VisualDiffResult = await workflow.execute_activity(
                    ACTIVITY_RUN_VISUAL_DIFF,
                    {
                        "work_item_id": input.work_item_id,
                        "tenant_id": input.tenant_id,
                        "base_screenshot_key": input.visual_baseline_key,
                        "candidate_screenshot_path": f"demos/{input.work_item_id}/screenshot.png",
                    },
                    result_type=VisualDiffResult,
                    start_to_close_timeout=timedelta(seconds=300),
                    retry_policy=RetryPolicy(maximum_attempts=2),
                )
                if vd.baseline_created and vd.diff_artifact_key:
                    # 1st run: WS-E publishes the candidate as the baseline and
                    # returns the key in diff_artifact_key (assumption
                    # documented in the README).
                    input.visual_baseline_key = vd.diff_artifact_key
                await self._audit(
                    "visual_diff_completed",
                    {"passed": vd.passed, "changed_pct": vd.changed_pct,
                     "baseline_created": vd.baseline_created, "reason": reason},
                )
            except ActivityError as exc:
                await self._audit(
                    "evidence_degraded",
                    {"stage": "run_visual_diff", "reason": reason,
                     "error": str(exc.cause or exc)[:300]},
                )
        await self._record_evidence(reason=reason, detail="ok")

    async def _post_preview_link(self, url: str, kind: str) -> None:
        """Plan 08 §D (D1) — posts/edits the originating surface's tracking
        comment with the clickable preview LINK. Reuses the MutableCommentWriter
        (C3) via post_tracking_comment (custom body). Best-effort — it never
        blocks (the audit ledger is the truth; the comment is convenience)."""
        input = self._input
        label = "frontend (UI)" if kind == "ui" else "service" if kind == "deployable" else "app"
        body = (
            f"🔗 **Preview ({label}) ready** — open it and decide:\n\n{url}\n\n"
            "_Ephemeral per-PR environment (Argo CD, TTL). Merging remains a "
            "human decision (P1)._"
        )
        try:
            await workflow.execute_activity(
                ACTIVITY_POST_TRACKING_COMMENT,
                {"work_item_id": input.work_item_id, "tenant_id": input.tenant_id,
                 "status": "pr_ready", "body": body},
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
            await self._audit("preview_link_posted", {"url": url, "kind": kind})
        except ActivityError:
            logger.warning("best-effort post_preview_link failed (url=%s)", url)

    async def _record_evidence(self, *, reason: str, detail: str) -> None:
        """Durable projection of the evidence state (migration 0014) —
        best-effort: the projection never blocks the flow (the audit ledger is
        the immutable source)."""
        input = self._input
        try:
            await workflow.execute_activity(
                LOCAL_ACTIVITY_RECORD_EVIDENCE,
                {
                    "work_item_id": input.work_item_id,
                    "tenant_id": input.tenant_id,
                    "preview_status": input.preview_status,
                    "preview_url": input.preview_url,
                    "demo_passed": input.evidence_passed,
                    "video_artifact_key": input.evidence_video_key,
                    "trace_artifact_key": input.evidence_trace_key,
                    "visual_baseline_key": input.visual_baseline_key,
                    "refresh_count": input.evidence_refreshes,
                    "last_refresh_reason": reason,
                    "detail": detail,
                },
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError:
            logger.warning("record_evidence_state failed; the projection is best-effort, the flow continues")
