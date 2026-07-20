"""WSE-E2-T5 — loop de fix-retries bounded L2 -> Coder (lógica determinística).

O workflow do WS-B é dono da ORQUESTRAÇÃO de estados (chamar Coder, re-L1, re-L2);
este módulo é a LÓGICA DE DECISÃO pura que ele consulta a cada veredito L2 (P1:
nenhum LLM decide — é aritmética sobre contador + budget):

    L2 reprova com objeção específica
        -> volta ao Coder (re-implementa) -> re-L1 -> re-L2
        -> repete, com contador limitado (`max_fix_retries`)
        -> esgotou retries OU budget -> ESCALA A OPERADOR (P6: nunca insiste,
           nunca corta no meio; falha limpa numa fronteira nomeada).

Cada iteração DEBITA budget (custo do turno do Coder + do turno L2). Nenhuma
iteração é permitida depois do cap de iterações OU do teto de custo esgotado (P6).
Tudo é auditado (P8) e o contador é durável (`wse_fix_loops`) para sobreviver a
crash/replay do workflow.
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
    iterations: int = 0  # nº de retornos ao Coder já consumidos
    spent_usd: float = 0.0
    exhausted: bool = False


class FixLoopDecision(BaseModel):
    action: Action
    reason: str
    objections: list[str] = []
    iterations_used: int
    iterations_remaining: int
    spent_usd: float
    budget_remaining_usd: float | None  # None = sem teto de custo configurado


def load_state(work_item_id: str, tenant_id: str) -> FixLoopState:
    """Estado durável do loop (cria em memória zerado se ainda não existe)."""
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
    """Decisão pura (sem efeitos colaterais) a partir do veredito L2 + estado.

    - L2 aprova            -> proceed (segue para o CI, P5).
    - L2 reprova e ainda há retries E budget -> retry_coder (com as objeções).
    - retries esgotados    -> escalate_operator.
    - budget esgotado      -> escalate_operator (P6: não gasta além do teto).
    """
    cfg = cfg or L2Config()
    remaining = max(0, cfg.max_fix_retries - state.iterations)
    budget_left = _budget_remaining(state, cfg)

    if verdict.passed:
        action: Action = "proceed"
        reason = "L2 aprovou o diff (plan+diff, contexto fresco)"
    elif remaining <= 0:
        action = "escalate_operator"
        reason = (
            f"objeções L2 persistem após {state.iterations} retornos ao Coder "
            f"(cap={cfg.max_fix_retries}) — escalando a operador (P6)"
        )
    elif budget_left is not None and budget_left <= 0:
        action = "escalate_operator"
        reason = (
            f"budget do loop esgotado (spent={state.spent_usd:.4f} >= "
            f"cap={cfg.budget_cap_usd:.4f} USD) — escalando a operador (P6)"
        )
    else:
        action = "retry_coder"
        reason = f"L2 reprovou; reenviando ao Coder ({remaining} retorno(s) restante(s))"

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
    """Debita o custo de UMA iteração de fix e incrementa o contador (durável).

    Guard P6 (belt-and-suspenders): recusa debitar se o loop já estava com o cap
    de iterações OU de budget esgotado — nesse caso o caller deveria ter escalado,
    não iniciado outra iteração. Levanta `FixLoopBudgetExceeded`."""
    cfg = cfg or L2Config()
    if state.iterations >= cfg.max_fix_retries:
        raise FixLoopBudgetExceeded(
            f"cap de {cfg.max_fix_retries} retries já atingido — não inicie outra iteração (P6)"
        )
    if cfg.budget_cap_usd > 0 and state.spent_usd >= cfg.budget_cap_usd:
        raise FixLoopBudgetExceeded(
            f"budget cap {cfg.budget_cap_usd:.4f} USD já atingido — não gaste mais (P6)"
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
    """Marca o loop como exausto (escalado a operador). Idempotente."""
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
    """P6 — tentativa de iniciar uma iteração de fix após o cap/budget esgotado."""
