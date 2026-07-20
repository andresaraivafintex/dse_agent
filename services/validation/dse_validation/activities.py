"""Activities Temporal do WS-E, registradas com os nomes de
`dse_contracts.activities` (ACTIVITY_RUN_L1_PIPELINE, ACTIVITY_FINALIZE_PR,
ACTIVITY_CONSUME_CI_STATUS) para que o Worker único do WS-B
(`services/orchestrator/worker.py`) as importe e registre.

Cada `@activity.defn` aqui é só um wrapper fino: monta os objetos reais
(executor a partir do `SandboxHandle`, `GitHubClient` a partir das env vars)
e chama a função core testável do módulo correspondente. Os testes deste
workstream chamam as funções core diretamente com fakes injetados — nunca
precisam do runtime do Temporal nem do Docker real para validar a LÓGICA
(mas o teste de review_signal roda contra o Temporal real, ver README).

Import defensivo: se `temporalio` não estiver instalado no ambiente que
importar este módulo, o resto de `dse_validation` continua utilizável (os
testes de lógica pura não dependem do decorator `@activity.defn`).
"""
from __future__ import annotations

from dse_contracts import (
    ACTIVITY_CONSUME_CI_STATUS,
    ACTIVITY_FINALIZE_PR,
    ACTIVITY_RUN_L1_PIPELINE,
    CiStatusResult,
    L1Result,
    L2Verdict,
    PlanArtifact,
    PrRef,
    SandboxHandle,
)
from pydantic import BaseModel, Field

from dse_validation.config import GitHubConfig, L1Config, L2Config
from dse_validation.github.ci_status import consume_ci_status_core
from dse_validation.github.client import build_github_client
from dse_validation.github.pr_finalizer import adopt_pr_core, finalize_pr_core
from dse_validation.l1.pipeline import run_l1_pipeline_core
from dse_validation.l2 import fix_loop as _fix_loop
from dse_validation.l2.l2_review import run_l2_review
from dse_validation.l2.session import L2ReviewInput, L2ReviewSession, build_l2_session
from dse_validation.sandbox_exec import executor_for_handle

# Nomes de Activity que o WS-E é dono na Fase 2. `ACTIVITY_RUN_L2_REVIEW`
# (dse_contracts) é a SESSÃO L2, dona do WS-C — o WS-E NÃO a registra; o WS-E
# registra a ORQUESTRAÇÃO em torno dela (recording de veredito/custo, decisão do
# loop de fix-retries, adoção de PR no modo estrito). Nomes distintos para não
# colidirem no Worker único.
WSE_ACTIVITY_RUN_L2_REVIEW = "wse_run_l2_review"  # orquestra a sessão + grava evidência
WSE_ACTIVITY_RECORD_FIX_LOOP = "wse_record_fix_loop"
WSE_ACTIVITY_ADOPT_PR = "wse_adopt_pr"

try:
    from temporalio import activity

    _HAS_TEMPORAL = True
except ImportError:  # pragma: no cover
    _HAS_TEMPORAL = False


# ---------------------------------------------------------------------------
# Modelos de input — Temporal Activities recebem 1 argumento pydantic único
# (facilita versionamento futuro sem quebrar a assinatura posicional).
# ---------------------------------------------------------------------------
class RunL1PipelineInput(BaseModel):
    sandbox: SandboxHandle
    plan: PlanArtifact
    tenant_id: str
    base_branch: str
    target_dir: str = "."
    repo_dir: str = "/workspace/repo"


class FinalizePrInput(BaseModel):
    work_item_id: str
    tenant_id: str
    repo: str
    branch: str
    base_branch: str
    summary: str
    risk_class: str = "low"
    evidence_url: str = ""
    issue_ref: dict | None = None
    sandbox: SandboxHandle | None = None
    repo_dir: str = "/workspace/repo"
    # Fase 2 (WSE-E3-T8): modo estrito. Se None, resolve por repo/tenant via
    # StrictModeConfig; se explicitamente setado, ganha. `surface_ref` é a
    # superfície do tracking comment onde postar o compare link.
    strict_mode: bool | None = None
    surface_ref: dict | None = None


class RunL2ReviewInput(BaseModel):
    """WSE-E2-T4. P3: só plan+diff atravessam — sem histórico do Coder."""

    work_item_id: str
    tenant_id: str
    plan: PlanArtifact
    diff: str
    iteration: int = 0
    l1_passed: bool = True  # guard cheapest-first (P5); o workflow passa o L1Result.passed


class RecordFixLoopInput(BaseModel):
    """WSE-E2-T5 — espelha o contador durável do loop mantido pelo workflow
    (WS-B é dono do estado; esta activity persiste evidência + audita)."""

    work_item_id: str
    tenant_id: str
    action: str  # "retry_coder" | "escalate_operator"
    iterations: int
    coder_cost_usd: float = 0.0
    l2_cost_usd: float = 0.0
    reason: str = ""
    objections: list[str] = Field(default_factory=list)


class AdoptPrInput(BaseModel):
    """WSE-E3-T8 — humano abriu o PR a partir do compare link; adota (mesmo WI)."""

    work_item_id: str
    tenant_id: str
    repo: str
    branch: str
    pr_number: int | None = None
    pr_url: str | None = None


class ConsumeCiStatusInput(BaseModel):
    work_item_id: str
    tenant_id: str
    repo: str
    pr_number: int
    ref: str = Field(description="commit sha (ou nome de branch) para consultar check-runs")


def _run_l1_pipeline(inp: RunL1PipelineInput) -> L1Result:
    executor = executor_for_handle(inp.sandbox, repo_dir=inp.repo_dir)
    return run_l1_pipeline_core(
        executor=executor,
        work_item_id=inp.sandbox.work_item_id,
        tenant_id=inp.tenant_id,
        plan=inp.plan,
        base_branch=inp.base_branch,
        target_dir=inp.target_dir,
        cfg=L1Config(),
    )


def _finalize_pr(inp: FinalizePrInput) -> PrRef:
    from dse_validation.config import StrictModeConfig
    from dse_validation.db import PostgresCommentStateStore
    from dse_validation.github.comment_backend import GitHubCommentBackend

    try:
        from dse_contracts.mutable_comment import MutableCommentWriter
    except ImportError:  # pragma: no cover
        MutableCommentWriter = None

    github_client = build_github_client(GitHubConfig())
    executor = executor_for_handle(inp.sandbox, repo_dir=inp.repo_dir) if inp.sandbox else None
    if executor is None:
        raise ValueError("finalize_pr requer um SandboxHandle válido para dar `git push`")

    strict = inp.strict_mode
    if strict is None:
        strict = StrictModeConfig().is_strict_for(inp.tenant_id, inp.repo)

    comment_writer = None
    if strict and inp.surface_ref is not None and MutableCommentWriter is not None:
        comment_writer = MutableCommentWriter(
            GitHubCommentBackend(github_client), PostgresCommentStateStore(), surface="github_pr"
        )

    return finalize_pr_core(
        executor=executor,
        github_client=github_client,
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        repo=inp.repo,
        branch=inp.branch,
        base_branch=inp.base_branch,
        summary=inp.summary,
        risk_class=inp.risk_class,
        evidence_url=inp.evidence_url,
        issue_ref=inp.issue_ref,
        strict_mode=strict,
        comment_writer=comment_writer,
        surface_ref=inp.surface_ref,
    )


def _run_l2_review(inp: RunL2ReviewInput, session: L2ReviewSession | None = None) -> L2Verdict:
    # P5 cheapest-first: L2 só roda depois do L1 verde. O workflow passa
    # `l1_passed`; se falso, falha limpa na fronteira (P6) em vez de gastar L2.
    if not inp.l1_passed:
        raise ValueError(
            f"L2 não pode rodar antes do L1 verde (cheapest-first/P5) para {inp.work_item_id}"
        )
    session = session or build_l2_session()
    review_input = L2ReviewInput(
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        plan=inp.plan,
        diff=inp.diff,  # P3: só plan+diff; L2ReviewInput não tem campo de histórico do Coder
        iteration=inp.iteration,
    )
    return run_l2_review(
        session,
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        inp=review_input,
        iteration=inp.iteration,
    )


def _record_fix_loop(inp: RecordFixLoopInput) -> dict:
    state = _fix_loop.FixLoopState(
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        iterations=max(0, inp.iterations - 1),  # estado ANTES desta iteração
    )
    if inp.action == "retry_coder":
        new_state = _fix_loop.register_retry(
            state, coder_cost_usd=inp.coder_cost_usd, l2_cost_usd=inp.l2_cost_usd
        )
    elif inp.action == "escalate_operator":
        new_state = _fix_loop.escalate_to_operator(
            state.model_copy(update={"iterations": inp.iterations}),
            reason=inp.reason,
            objections=inp.objections,
        )
    else:  # pragma: no cover - guard
        raise ValueError(f"ação de fix-loop desconhecida: {inp.action}")
    return new_state.model_dump()


def _adopt_pr(inp: AdoptPrInput) -> PrRef | None:
    github_client = build_github_client(GitHubConfig())
    return adopt_pr_core(
        github_client=github_client,
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        repo=inp.repo,
        branch=inp.branch,
        pr_number=inp.pr_number,
        pr_url=inp.pr_url,
    )


def _consume_ci_status(inp: ConsumeCiStatusInput) -> CiStatusResult:
    github_client = build_github_client(GitHubConfig())
    return consume_ci_status_core(
        github_client=github_client,
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        repo=inp.repo,
        pr_number=inp.pr_number,
        ref=inp.ref,
    )


if _HAS_TEMPORAL:

    @activity.defn(name=ACTIVITY_RUN_L1_PIPELINE)
    async def run_l1_pipeline(inp: RunL1PipelineInput) -> L1Result:
        return _run_l1_pipeline(inp)

    @activity.defn(name=ACTIVITY_FINALIZE_PR)
    async def finalize_pr(inp: FinalizePrInput) -> PrRef:
        return _finalize_pr(inp)

    @activity.defn(name=ACTIVITY_CONSUME_CI_STATUS)
    async def consume_ci_status(inp: ConsumeCiStatusInput) -> CiStatusResult:
        return _consume_ci_status(inp)

    @activity.defn(name=WSE_ACTIVITY_RUN_L2_REVIEW)
    async def wse_run_l2_review(inp: RunL2ReviewInput) -> L2Verdict:
        return _run_l2_review(inp)

    @activity.defn(name=WSE_ACTIVITY_RECORD_FIX_LOOP)
    async def wse_record_fix_loop(inp: RecordFixLoopInput) -> dict:
        return _record_fix_loop(inp)

    @activity.defn(name=WSE_ACTIVITY_ADOPT_PR)
    async def wse_adopt_pr(inp: AdoptPrInput) -> PrRef | None:
        return _adopt_pr(inp)

    ALL_ACTIVITIES = [
        run_l1_pipeline,
        finalize_pr,
        consume_ci_status,
        wse_run_l2_review,
        wse_record_fix_loop,
        wse_adopt_pr,
    ]
else:  # pragma: no cover
    ALL_ACTIVITIES = []

# Alias esperado pelo loader defensivo do worker unico (services/orchestrator/
# src/dse_orchestrator/worker.py:_load_cross_workstream_activities), que
# procura `ACTIVITIES` (nao `ALL_ACTIVITIES`) neste modulo.
ACTIVITIES = ALL_ACTIVITIES
