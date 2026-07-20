"""WSE-E2-T4 — orquestração de 1 turno da revisão L2 (contexto fresco).

Ordenação cheapest-first (P5): a validação vai L1 determinístico -> L2 modelo ->
L3 CI -> humano. Este módulo é o degrau L2: só deve rodar DEPOIS do L1 verde e
ANTES do CI. `guard_l2_after_l1` torna essa precondição explícita e auditável em
vez de confiar que o caller lembrou da ordem.

O que o WS-E é dono aqui (o WS-B chama, o WS-C provê a sessão):
  1. chamar a sessão L2 (só com plan+diff — P3, garantido pelo tipo `L2ReviewInput`);
  2. registrar o veredito + custo em `wse_l2_reviews` (evidência, P8);
  3. emitir 1 linha de audit (`l2_review_run`).

Nenhum LLM decide fluxo aqui (P1): a sessão só PRODUZ um veredito estruturado;
o que fazer com ele (seguir p/ CI, voltar ao Coder, escalar) é a lógica
determinística de `fix_loop`, chamada pelo workflow do WS-B.
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
    """Levantado se tentarem rodar L2 antes do L1 estar verde (viola P5)."""


def guard_l2_after_l1(l1_result: L1Result) -> None:
    """P5 cheapest-first: nunca gasta um turno L2 (modelo, caro) se o L1
    (determinístico, barato) ainda não passou. Falha limpa na fronteira (P6)."""
    if not l1_result.passed:
        raise L2PreconditionError(
            f"L2 não pode rodar: L1 ainda não está verde para {l1_result.work_item_id} "
            f"(cheapest-first/P5). Falhas L1: "
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
    """Executa 1 turno L2 e registra evidência+custo. `inp` já carrega SÓ
    plan+diff (P3). `iteration` é o índice do turno no loop de fix-retries."""
    verdict = session.review(inp)
    # normaliza para o work_item corrente (o fake pode ter vindo genérico)
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
