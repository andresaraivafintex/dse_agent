"""WSE-E2-T4 — orchestration of 1 turn of the L2 review (fresh context).

Cheapest-first ordering (P5): validation goes deterministic L1 -> model L2 ->
CI L3 -> human. This module is the L2 rung: it must only run AFTER L1 is green
and BEFORE CI. `guard_l2_after_l1` makes that precondition explicit and auditable
instead of trusting that the caller remembered the order.

What WS-E owns here (WS-B calls it, WS-C provides the session):
  1. call the L2 session (with plan+diff only — P3, enforced by the `L2ReviewInput` type);
  2. record the verdict + cost in `wse_l2_reviews` (evidence, P8);
  3. emit 1 audit line (`l2_review_run`).

No LLM decides flow here (P1): the session only PRODUCES a structured verdict;
what to do with it (proceed to CI, go back to the Coder, escalate) is the
deterministic logic in `fix_loop`, called by the WS-B workflow.
"""
from __future__ import annotations

from dse_contracts import L1Result, L2Verdict

from dse_validation import db
from dse_validation.l2.session import L2ReviewInput, L2ReviewSession

try:
    from dse_audit import emit as audit_emit
except ImportError:  # pragma: no cover
    audit_emit = None


class L2PreconditionError(RuntimeError):
    """Raised if something tries to run L2 before L1 is green (violates P5)."""


def guard_l2_after_l1(l1_result: L1Result) -> None:
    """P5 cheapest-first: never spend an L2 turn (model, expensive) if L1
    (deterministic, cheap) has not passed yet. Clean failure at the boundary (P6)."""
    if not l1_result.passed:
        raise L2PreconditionError(
            f"L2 cannot run: L1 is not green yet for {l1_result.work_item_id} "
            f"(cheapest-first/P5). L1 failures: "
            f"{[f.check for f in l1_result.findings if not f.passed]}"
        )


def run_l2_review(
    session: L2ReviewSession,
    *,
    work_item_id: str,
    tenant_id: str,
    inp: L2ReviewInput,
    iteration: int = 0,
    actor: str = "system:validation",
    persist: bool = True,
) -> L2Verdict:
    """Runs 1 L2 turn and records evidence+cost. `inp` already carries ONLY
    plan+diff (P3). `iteration` is the turn index within the fix-retry loop."""
    verdict = session.review(inp)
    # normalize to the current work_item (the fake may have returned a generic one)
    verdict = verdict.model_copy(update={"work_item_id": work_item_id})

    if persist:
        db.record_l2_review(
            work_item_id=work_item_id,
            tenant_id=tenant_id,
            iteration=iteration,
            passed=verdict.passed,
            objections=verdict.objections,
            cost_usd=verdict.cost_usd,
        )
    if audit_emit is not None:
        audit_emit(
            actor=actor,
            action="l2_review_run",
            tenant_id=tenant_id,
            work_item_id=work_item_id,
            details={
                "iteration": iteration,
                "passed": verdict.passed,
                "objections": verdict.objections,
                "cost_usd": verdict.cost_usd,
            },
        )
    return verdict
