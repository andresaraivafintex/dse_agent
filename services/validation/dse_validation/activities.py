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
    ACTIVITY_VERIFY_MERGE_STATE,
    CiStatusResult,
    L1Result,
    L2Verdict,
    MergeVerification,
    PlanArtifact,
    PrRef,
    SandboxHandle,
    VerifyMergeInput,
)
from dse_contracts.activities import (
    ACTIVITY_PUBLISH_ARTIFACT,
    ACTIVITY_RUN_DEMO_EVIDENCE,
    ACTIVITY_RUN_VISUAL_DIFF,
    ACTIVITY_TRIGGER_PREVIEW,
    ACTIVITY_UPDATE_BASE_BRANCH,
    ArtifactRef,
    DemoEvidenceResult,
    PreviewRef,
    PublishArtifactInput,
    RunDemoEvidenceInput,
    RunVisualDiffInput,
    TriggerPreviewInput,
    UpdateBaseBranchInput,
    UpdateBaseBranchResult,
    VisualDiffResult,
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

# Fase 3 — os 4 nomes do CONTRATO (dse_contracts) são do WS-E (dono declarado
# no próprio contrato): run_demo_evidence, publish_artifact, trigger_preview,
# run_visual_diff. Os auxiliares abaixo têm prefixo wse_ (não-contratuais).
WSE_ACTIVITY_QUARANTINE_ARTIFACTS = "wse_quarantine_artifacts"
WSE_ACTIVITY_REAP_PREVIEWS = "wse_reap_previews"
WSE_ACTIVITY_SHOULD_REFRESH_EVIDENCE = "wse_should_refresh_evidence"
WSE_ACTIVITY_PUBLISH_EVIDENCE = "wse_publish_evidence"

# Fase 4 — ACTIVITY_UPDATE_BASE_BRANCH (dse_contracts) é do WS-E (merge-base,
# WSE-E6-T16). O episódio de review-feedback (WSE-E6-T18) é auxiliar (prefixo
# wse_, não-contratual — só grava o episódio; a promoção é do WS-C).
WSE_ACTIVITY_RECORD_REVIEW_EPISODE = "wse_record_review_episode"

try:
    from temporalio import activity

    _HAS_TEMPORAL = True
except ImportError:  # pragma: no cover
    _HAS_TEMPORAL = False


# ---------------------------------------------------------------------------
# Modelos de input — Temporal Activities recebem 1 argumento pydantic único
# (facilita versionamento futuro sem quebrar a assinatura posicional).
#
# RunL1PipelineInput: usa o CANÔNICO de dse_contracts (achado do disparo real
# 2026-07-22: um shadow local ficou para trás sem work_item_id/base_sha —
# AttributeError em produção enquanto os testes de contrato passavam no
# canônico). Nunca redefina modelos de contrato localmente.
# ---------------------------------------------------------------------------
from dse_contracts.activities import RunL1PipelineInput  # noqa: E402


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
    """Lição de robustez (auditoria pós-S7, observada AO VIVO): payloads de
    activity ficam GRAVADOS na história do Temporal — um call site antigo que
    agendou esta activity com {work_item_id, pr_number} retenta com esse
    payload PARA SEMPRE; corrigir o call site não cura workflows em voo. Por
    isso tenant_id/repo/ref têm default vazio e são RESOLVIDOS do banco pela
    activity quando ausentes (work_items + wse_pr_tracking) — payload antigo
    decodifica e o workflow se auto-cura no próximo retry."""

    work_item_id: str
    tenant_id: str = ""
    repo: str = ""
    pr_number: int
    ref: str = Field(default="", description="commit sha (ou nome de branch) para consultar check-runs")
    # Fase 3 (WSE-E4-T9b) — aditivo: quando `surface_ref` vem preenchido, o
    # consumo L3 reflete o status no tracking comment único do PR e habilita
    # targeted re-runs/episódios de repair. Payloads da Fase 1/2 (sem o campo)
    # continuam decodificando igual.
    surface_ref: dict | None = None


class QuarantineArtifactsInput(BaseModel):
    """WSE-E5-T12 — aceite do WS-F: artefato de work item quarantinado é movido
    p/ prefixo de quarentena e o acesso é invalidado antes do TTL."""

    work_item_id: str
    tenant_id: str
    actor: str = "system:validation"


class ShouldRefreshEvidenceInput(BaseModel):
    """WSE-E5-T14 / ADR-26 — contrato de decisão de debounce consumido pelo
    workflow do WS-B (em construção paralela): re-gerar evidência SÓ a pedido
    humano explícito ou commit que muda comportamento. Retorno:
    {"refresh": bool, "reason": str} — decisão 100% determinística (P1)."""

    work_item_id: str
    tenant_id: str
    commit_sha: str
    files_changed: list[str] = Field(default_factory=list)
    human_requested: bool = False


class PublishEvidenceInput(BaseModel):
    """WSE-E5-T14 — publicação consolidada (vídeo/preview/diff/trace num único
    tracking comment) com debounce embutido."""

    work_item_id: str
    tenant_id: str
    commit_sha: str
    surface_ref: dict
    pr_number: int | None = None
    files_changed: list[str] = Field(default_factory=list)
    human_requested: bool = False


class RecordReviewEpisodeInput(BaseModel):
    """WSE-E6-T18 — grava 1 episódio de skill-learning de review feedback ACEITO.
    NENHUMA skill é criada/ativada (só o episódio; promoção é do WS-C)."""

    work_item_id: str
    tenant_id: str
    reviewer: str
    comment_body: str
    pr_number: int | None = None
    path: str | None = None
    diff_hunk: str | None = None
    accepted: bool = True


def _run_l1_pipeline(inp: RunL1PipelineInput) -> L1Result:
    # Boundary bug corrigido no disparo real (2026-07-22): o core mudou para
    # base_sha/head_sha (sha-bound-validation-inputs-v1) e este wrapper seguia
    # passando base_branch — os testes chamam o CORE direto e nunca viram o
    # boundary (test_l1_wrapper_matches_core_signature agora trava isso).
    executor = executor_for_handle(inp.sandbox, repo_dir=inp.repo_dir)
    # cfg=None → o core carrega o MANIFESTO CONFIÁVEL do repo
    # (.dse/validation.json lido do base_sha imutável). Passar L1Config()
    # default aqui reprovava TUDO (manifest NOT_CONFIGURED) — achado do
    # disparo real; o L1 de verdade é o do manifesto commitado no repo alvo.
    return run_l1_pipeline_core(
        executor=executor,
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        plan=inp.plan,
        base_sha=inp.base_sha,
        head_sha=inp.head_sha,
        target_dir=inp.target_dir,
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


def _verify_merge_state(inp: VerifyMergeInput, github_client=None) -> MergeVerification:
    """Plano 08 §F (F1) — confirma na API do GitHub que o PR está REALMENTE
    merged (e, se dado, com o head_sha esperado). Fail-safe: qualquer erro/dúvida
    => verified=False (o workflow nunca conclui como done com base nisso). O
    `github_client` é injetável para teste; em produção vem das env vars."""
    client = github_client or build_github_client(GitHubConfig())
    try:
        pr = client.get_pull_request(inp.repo, inp.pr_number)
    except Exception as exc:  # rede/credencial: fail-safe (não verificado)
        return MergeVerification(verified=False, reason=f"api_error:{type(exc).__name__}")
    if pr is None:
        return MergeVerification(exists=False, verified=False, reason="pr_not_found")
    merged = bool(pr.get("merged"))
    head_sha = pr.get("head_sha")
    merged_by = pr.get("merged_by")
    if not merged:
        return MergeVerification(
            exists=True, merged=False, head_sha=head_sha, merged_by=merged_by,
            verified=False, reason=f"not_merged(state={pr.get('state')})",
        )
    if inp.expected_head_sha and head_sha and inp.expected_head_sha != head_sha:
        return MergeVerification(
            exists=True, merged=True, head_sha=head_sha, merged_by=merged_by,
            merge_commit_sha=pr.get("merge_commit_sha"),
            verified=False, reason="head_sha_mismatch",
        )
    return MergeVerification(
        exists=True, merged=True, head_sha=head_sha, merged_by=merged_by,
        merge_commit_sha=pr.get("merge_commit_sha"), verified=True, reason="ok",
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


def _resolve_ci_input_gaps(inp: ConsumeCiStatusInput) -> ConsumeCiStatusInput:
    """Preenche tenant_id/repo/ref ausentes (payload antigo na história do
    Temporal — ver docstring do modelo) a partir de work_items +
    wse_pr_tracking. Deterministico; no-op quando o payload já veio completo."""
    if inp.tenant_id and inp.repo and inp.ref:
        return inp
    from dse_validation.db import get_connection
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT wi.tenant_id, t.repo, t.branch
            FROM work_items wi
            LEFT JOIN wse_pr_tracking t ON t.work_item_id = wi.id
            WHERE wi.id = %s
            ORDER BY t.created_at DESC NULLS LAST LIMIT 1
            """,
            (inp.work_item_id,),
        )
        row = cur.fetchone()
    if row is None:
        return inp
    tenant_id, repo, branch = row
    return inp.model_copy(update={
        "tenant_id": inp.tenant_id or (tenant_id or ""),
        "repo": inp.repo or (repo or ""),
        "ref": inp.ref or (branch or ""),
    })


def _consume_ci_status(inp: ConsumeCiStatusInput) -> CiStatusResult:
    inp = _resolve_ci_input_gaps(inp)
    github_client = build_github_client(GitHubConfig())
    if inp.surface_ref is None:
        # comportamento Fase 1/2 inalterado (poll + agregação + persistência)
        return consume_ci_status_core(
            github_client=github_client,
            work_item_id=inp.work_item_id,
            tenant_id=inp.tenant_id,
            repo=inp.repo,
            pr_number=inp.pr_number,
            ref=inp.ref,
        )
    # Fase 3 (WSE-E4-T9b): L3 completo — reflexão no tracking comment +
    # targeted re-runs em fix commit + episódios de CI-repair.
    from dse_contracts.mutable_comment import MutableCommentWriter

    from dse_validation.db import PostgresCommentStateStore
    from dse_validation.github.comment_backend import GitHubCommentBackend
    from dse_validation.github.l3 import consume_ci_status_l3

    writer = MutableCommentWriter(
        GitHubCommentBackend(github_client), PostgresCommentStateStore(), surface="github_pr_ci"
    )
    return consume_ci_status_l3(
        github_client=github_client,
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        repo=inp.repo,
        pr_number=inp.pr_number,
        ref=inp.ref,
        comment_writer=writer,
        surface_ref=inp.surface_ref,
    )


# ---------------------------------------------------------------------------
# Fase 4 — merge-base (WSE-E6-T16) e episódio de review-feedback (WSE-E6-T18)
# ---------------------------------------------------------------------------
def _update_base_branch(inp: UpdateBaseBranchInput) -> UpdateBaseBranchResult:
    """Wrapper de Activity: resolve o workspace git e as threads de review
    ancoradas (via PR rastreado + GitHub client) e chama o core determinístico.
    Como o LocalFakeSandbox no L1, os TESTES chamam `update_base_branch_core`
    diretamente com um bare repo local real — este wrapper é o seam de
    integração com o WS-C (workspace do sandbox) + GitHub App reais."""
    from dse_validation.github.client import build_github_client
    from dse_validation.merge_base import MergeBaseConfig, update_base_branch_core

    _origin, workspace_dir = MergeBaseConfig().locations(inp.work_item_id)

    # threads de review ancoradas em commits — resolvidas pelo PR rastreado.
    anchored: list[str] = []
    # fix (observado ao vivo no review loop): `db` nunca foi importado neste
    # módulo — NameError em todo update_base_branch real. Import local, no
    # estilo dos demais deste arquivo.
    from dse_validation import db as _db
    tracked = _db.get_tracked_pr(inp.work_item_id)
    pr_number = tracked.get("pr_number") if tracked else None
    if pr_number is not None:
        github_client = build_github_client(GitHubConfig())
        for t in github_client.list_review_threads(inp.repo, int(pr_number)):
            sha = t.get("original_commit_id") or t.get("commit_id")
            if sha:
                anchored.append(sha)

    return update_base_branch_core(
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        repo=inp.repo,
        branch=inp.branch,
        base_branch=inp.base_branch,
        workspace_dir=workspace_dir,
        first_human_review_done=inp.first_human_review_done,
        anchored_review_shas=anchored,
    )


def _record_review_episode(inp: RecordReviewEpisodeInput) -> dict | None:
    from dse_validation.review_learning import record_review_feedback_episode

    return record_review_feedback_episode(
        tenant_id=inp.tenant_id,
        work_item_id=inp.work_item_id,
        pr_number=inp.pr_number,
        reviewer=inp.reviewer,
        comment_body=inp.comment_body,
        path=inp.path,
        diff_hunk=inp.diff_hunk,
        accepted=inp.accepted,
    )


# ---------------------------------------------------------------------------
# Fase 3 — cores das Activities de evidência (contrato)
# ---------------------------------------------------------------------------
def _publish_artifact(inp: PublishArtifactInput) -> ArtifactRef:
    from dse_validation.evidence.garage import publish_artifact_core

    return publish_artifact_core(inp)


def _run_demo_evidence(inp: RunDemoEvidenceInput) -> DemoEvidenceResult:
    from dse_validation.evidence.demo import run_demo_evidence_core

    return run_demo_evidence_core(inp)


def _trigger_preview(inp: TriggerPreviewInput) -> PreviewRef:
    from dse_validation.preview.argocd import trigger_preview_core

    return trigger_preview_core(inp)


def _run_visual_diff(inp: RunVisualDiffInput) -> VisualDiffResult:
    from dse_validation.evidence.visual_diff import run_visual_diff_core

    return run_visual_diff_core(inp)


def _quarantine_artifacts(inp: QuarantineArtifactsInput) -> list[str]:
    from dse_validation.evidence.garage import quarantine_artifacts_for_work_item

    return quarantine_artifacts_for_work_item(inp.work_item_id, actor=inp.actor)


def _reap_previews() -> list[str]:
    from dse_validation.preview.argocd import reap_expired_previews

    return reap_expired_previews()


def _should_refresh_evidence(inp: ShouldRefreshEvidenceInput) -> dict:
    from dse_validation.evidence.publication import should_refresh_evidence

    decision = should_refresh_evidence(
        work_item_id=inp.work_item_id,
        commit_sha=inp.commit_sha,
        files_changed=inp.files_changed or None,
        human_requested=inp.human_requested,
    )
    return {"refresh": decision.refresh, "reason": decision.reason}


def _publish_evidence(inp: PublishEvidenceInput) -> dict:
    from dse_contracts.mutable_comment import MutableCommentWriter

    from dse_validation.db import PostgresCommentStateStore
    from dse_validation.evidence.publication import publish_evidence_bundle
    from dse_validation.github.comment_backend import GitHubCommentBackend

    github_client = build_github_client(GitHubConfig())
    writer = MutableCommentWriter(
        GitHubCommentBackend(github_client), PostgresCommentStateStore(), surface="github_pr_evidence"
    )
    return publish_evidence_bundle(
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        commit_sha=inp.commit_sha,
        comment_writer=writer,
        surface_ref=inp.surface_ref,
        pr_number=inp.pr_number,
        files_changed=inp.files_changed or None,
        human_requested=inp.human_requested,
    )


if _HAS_TEMPORAL:

    @activity.defn(name=ACTIVITY_RUN_L1_PIPELINE)
    async def run_l1_pipeline(inp: RunL1PipelineInput) -> L1Result:
        return _run_l1_pipeline(inp)

    @activity.defn(name=ACTIVITY_FINALIZE_PR)
    async def finalize_pr(inp: FinalizePrInput) -> PrRef:
        return _finalize_pr(inp)

    @activity.defn(name=ACTIVITY_VERIFY_MERGE_STATE)
    async def verify_merge_state(inp: VerifyMergeInput) -> MergeVerification:
        return _verify_merge_state(inp)

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

    # --- Fase 3: Activities de evidência do CONTRATO (dono: WS-E) ---
    @activity.defn(name=ACTIVITY_PUBLISH_ARTIFACT)
    async def publish_artifact(inp: PublishArtifactInput) -> ArtifactRef:
        return _publish_artifact(inp)

    @activity.defn(name=ACTIVITY_RUN_DEMO_EVIDENCE)
    async def run_demo_evidence(inp: RunDemoEvidenceInput) -> DemoEvidenceResult:
        return _run_demo_evidence(inp)

    @activity.defn(name=ACTIVITY_TRIGGER_PREVIEW)
    async def trigger_preview(inp: TriggerPreviewInput) -> PreviewRef:
        return _trigger_preview(inp)

    @activity.defn(name=ACTIVITY_RUN_VISUAL_DIFF)
    async def run_visual_diff(inp: RunVisualDiffInput) -> VisualDiffResult:
        return _run_visual_diff(inp)

    # --- Fase 3: auxiliares (não-contratuais, prefixo wse_) ---
    @activity.defn(name=WSE_ACTIVITY_QUARANTINE_ARTIFACTS)
    async def wse_quarantine_artifacts(inp: QuarantineArtifactsInput) -> list[str]:
        return _quarantine_artifacts(inp)

    @activity.defn(name=WSE_ACTIVITY_REAP_PREVIEWS)
    async def wse_reap_previews() -> list[str]:
        return _reap_previews()

    @activity.defn(name=WSE_ACTIVITY_SHOULD_REFRESH_EVIDENCE)
    async def wse_should_refresh_evidence(inp: ShouldRefreshEvidenceInput) -> dict:
        return _should_refresh_evidence(inp)

    @activity.defn(name=WSE_ACTIVITY_PUBLISH_EVIDENCE)
    async def wse_publish_evidence(inp: PublishEvidenceInput) -> dict:
        return _publish_evidence(inp)

    # --- Fase 4: merge-base (contrato) + episódio de review-feedback (aux) ---
    @activity.defn(name=ACTIVITY_UPDATE_BASE_BRANCH)
    async def update_base_branch(inp: UpdateBaseBranchInput) -> UpdateBaseBranchResult:
        return _update_base_branch(inp)

    @activity.defn(name=WSE_ACTIVITY_RECORD_REVIEW_EPISODE)
    async def wse_record_review_episode(inp: RecordReviewEpisodeInput) -> dict | None:
        return _record_review_episode(inp)

    ALL_ACTIVITIES = [
        run_l1_pipeline,
        finalize_pr,
        verify_merge_state,
        consume_ci_status,
        wse_run_l2_review,
        wse_record_fix_loop,
        wse_adopt_pr,
        # Fase 3
        publish_artifact,
        run_demo_evidence,
        trigger_preview,
        run_visual_diff,
        wse_quarantine_artifacts,
        wse_reap_previews,
        wse_should_refresh_evidence,
        wse_publish_evidence,
        # Fase 4
        update_base_branch,
        wse_record_review_episode,
    ]
else:  # pragma: no cover
    ALL_ACTIVITIES = []

# Alias esperado pelo loader defensivo do worker unico (services/orchestrator/
# src/dse_orchestrator/worker.py:_load_cross_workstream_activities), que
# procura `ACTIVITIES` (nao `ALL_ACTIVITIES`) neste modulo.
ACTIVITIES = ALL_ACTIVITIES
