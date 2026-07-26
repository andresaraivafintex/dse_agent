"""WSE-E2-T5 — bounded L2 -> Coder fix-retry loop (deterministic logic).

The WS-B workflow owns the state ORCHESTRATION (call the Coder, re-L1, re-L2);
this module is the pure DECISION LOGIC it consults on every L2 verdict (P1: no
LLM decides — it is arithmetic over a counter + budget):

    L2 rejects with a specific objection
        -> back to the Coder (re-implements) -> re-L1 -> re-L2
        -> repeat, with a bounded counter (`max_fix_retries`)
        -> retries OR budget exhausted -> ESCALATE TO OPERATOR (P6: never insist,
           never truncate midway; clean failure at a named boundary).

Every iteration DEBITS budget (cost of the Coder turn + the L2 turn). No
iteration is allowed after the iteration cap OR the cost ceiling is exhausted (P6).
Everything is audited (P8) and the counter is durable (`wse_fix_loops`) so it
survives workflow crash/replay.
"""
from __future__ import annotations

from typing import Literal

from dse_contracts import L2Verdict
from pydantic import BaseModel

from dse_validation import db
from dse_validation.config import L2Config

try:
    from dse_audit import emit as audit_emit
except ImportError:  # pragma: no cover
    audit_emit = None


Action = Literal["proceed", "retry_coder", "escalate_operator"]


class FixLoopState(BaseModel):
    work_item_id: str
    tenant_id: str
    iterations: int = 0  # number of Coder round-trips already consumed
    spent_usd: float = 0.0
    exhausted: bool = False


class FixLoopDecision(BaseModel):
    action: Action
    reason: str
    objections: list[str] = []
    iterations_used: int
    iterations_remaining: int
    spent_usd: float
    budget_remaining_usd: float | None  # None = no cost ceiling configured


def load_state(work_item_id: str, tenant_id: str) -> FixLoopState:
    """Durable loop state (creates a zeroed in-memory one if it does not exist yet)."""
    row = db.get_fix_loop(work_item_id)
    if row is None:
        return FixLoopState(work_item_id=work_item_id, tenant_id=tenant_id)
    return FixLoopState(
        work_item_id=row["work_item_id"],
        tenant_id=row["tenant_id"],
        iterations=row["iterations"],
        spent_usd=row["spent_usd"],
        exhausted=row["exhausted"],
    )


def _budget_remaining(state: FixLoopState, cfg: L2Config) -> float | None:
    if cfg.budget_cap_usd <= 0:
        return None
    return max(0.0, cfg.budget_cap_usd - state.spent_usd)


def decide_next_action(
    *, verdict: L2Verdict, state: FixLoopState, cfg: L2Config | None = None
) -> FixLoopDecision:
    """Pure decision (no side effects) from the L2 verdict + state.

    - L2 approves          -> proceed (move on to CI, P5).
    - L2 rejects and there are retries AND budget left -> retry_coder (with the objections).
    - retries exhausted    -> escalate_operator.
    - budget exhausted     -> escalate_operator (P6: never spend past the ceiling).
    """
    cfg = cfg or L2Config()
    remaining = max(0, cfg.max_fix_retries - state.iterations)
    budget_left = _budget_remaining(state, cfg)

    if verdict.passed:
        action: Action = "proceed"
        reason = "L2 approved the diff (plan+diff, fresh context)"
    elif remaining <= 0:
        action = "escalate_operator"
        reason = (
            f"L2 objections persist after {state.iterations} returns to the Coder "
            f"(cap={cfg.max_fix_retries}) — escalating to an operator (P6)"
        )
    elif budget_left is not None and budget_left <= 0:
        action = "escalate_operator"
        reason = (
            f"loop budget exhausted (spent={state.spent_usd:.4f} >= "
            f"cap={cfg.budget_cap_usd:.4f} USD) — escalating to an operator (P6)"
        )
    else:
        action = "retry_coder"
        reason = f"L2 rejected; sending back to the Coder ({remaining} return(s) left)"

    return FixLoopDecision(
        action=action,
        reason=reason,
        objections=list(verdict.objections),
        iterations_used=state.iterations,
        iterations_remaining=remaining,
        spent_usd=state.spent_usd,
        budget_remaining_usd=budget_left,
    )


def register_retry(
    state: FixLoopState,
    *,
    coder_cost_usd: float = 0.0,
    l2_cost_usd: float = 0.0,
    cfg: L2Config | None = None,
    persist: bool = True,
    actor: str = "system:validation",
) -> FixLoopState:
    """Debits the cost of ONE fix iteration and increments the counter (durably).

    P6 guard (belt-and-suspenders): refuses to debit if the loop had already hit
    the iteration cap OR exhausted its budget — in that case the caller should
    have escalated, not started another iteration. Raises `FixLoopBudgetExceeded`."""
    cfg = cfg or L2Config()
    if state.iterations >= cfg.max_fix_retries:
        raise FixLoopBudgetExceeded(
            f"cap of {cfg.max_fix_retries} retries already reached — do not start another iteration (P6)"
        )
    if cfg.budget_cap_usd > 0 and state.spent_usd >= cfg.budget_cap_usd:
        raise FixLoopBudgetExceeded(
            f"budget cap {cfg.budget_cap_usd:.4f} USD already reached — do not spend more (P6)"
        )

    new_state = state.model_copy(
        update={
            "iterations": state.iterations + 1,
            "spent_usd": round(state.spent_usd + coder_cost_usd + l2_cost_usd, 6),
        }
    )
    if persist:
        db.upsert_fix_loop(
            new_state.work_item_id,
            new_state.tenant_id,
            new_state.iterations,
            new_state.spent_usd,
            new_state.exhausted,
        )
    if audit_emit is not None:
        audit_emit(
            actor=actor,
            action="l2_fix_retry",
            tenant_id=new_state.tenant_id,
            work_item_id=new_state.work_item_id,
            details={
                "iteration": new_state.iterations,
                "coder_cost_usd": coder_cost_usd,
                "l2_cost_usd": l2_cost_usd,
                "spent_usd": new_state.spent_usd,
            },
        )
    return new_state


def escalate_to_operator(
    state: FixLoopState,
    *,
    reason: str,
    objections: list[str] | None = None,
    persist: bool = True,
    actor: str = "system:validation",
) -> FixLoopState:
    """Marks the loop as exhausted (escalated to an operator). Idempotent."""
    new_state = state.model_copy(update={"exhausted": True})
    if persist:
        db.upsert_fix_loop(
            new_state.work_item_id,
            new_state.tenant_id,
            new_state.iterations,
            new_state.spent_usd,
            True,
        )
    if audit_emit is not None:
        audit_emit(
            actor=actor,
            action="l2_fix_loop_exhausted",
            tenant_id=new_state.tenant_id,
            work_item_id=new_state.work_item_id,
            details={
                "reason": reason,
                "iterations": new_state.iterations,
                "spent_usd": new_state.spent_usd,
                "objections": objections or [],
            },
        )
    return new_state


class FixLoopBudgetExceeded(RuntimeError):
    """P6 — attempt to start a fix iteration after the cap/budget was exhausted."""
