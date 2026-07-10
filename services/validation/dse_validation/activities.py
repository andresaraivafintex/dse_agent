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
    PlanArtifact,
    PrRef,
    SandboxHandle,
)
from pydantic import BaseModel, Field

from dse_validation.config import GitHubConfig, L1Config
from dse_validation.github.ci_status import consume_ci_status_core
from dse_validation.github.client import build_github_client
from dse_validation.github.pr_finalizer import finalize_pr_core
from dse_validation.l1.pipeline import run_l1_pipeline_core
from dse_validation.sandbox_exec import executor_for_handle

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
    github_client = build_github_client(GitHubConfig())
    executor = executor_for_handle(inp.sandbox, repo_dir=inp.repo_dir) if inp.sandbox else None
    if executor is None:
        raise ValueError("finalize_pr requer um SandboxHandle válido para dar `git push`")
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

    ALL_ACTIVITIES = [run_l1_pipeline, finalize_pr, consume_ci_status]
else:  # pragma: no cover
    ALL_ACTIVITIES = []

# Alias esperado pelo loader defensivo do worker unico (services/orchestrator/
# src/dse_orchestrator/worker.py:_load_cross_workstream_activities), que
# procura `ACTIVITIES` (nao `ALL_ACTIVITIES`) neste modulo.
ACTIVITIES = ALL_ACTIVITIES
