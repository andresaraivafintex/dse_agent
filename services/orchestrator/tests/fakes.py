"""Activities FAKE que implementam a MESMA assinatura/nome/tipo de retorno
das Activities cross-workstream de `dse_contracts.activities` (donas: WS-C e
WS-E). Permitem provar a maquina de estados do workflow (WSB-E2-T3) sem
depender dos outros workstreams terem terminado suas implementacoes reais —
exatamente como pedido no enunciado da tarefa.

Postgres e Temporal em si NUNCA sao mockados aqui — so as duas fronteiras de
Activity que pertencem a outros workstreams. As Activities locais do WS-B
(`update_work_item_status`, `check_clarification_completeness`,
`emit_audit_event`) usadas pelos testes SAO as reais, batendo no Postgres
real da infra (ver `conftest.py`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError


def _maybe_fail_closed(state: "FakeControlPlane", name: str) -> None:
    """WSB-E5-T3b — simula uma recusa de politica fail-closed do caminho de
    modelo/egress (egress-proxy down, virtual key expirada, kill switch). Levanta
    um ApplicationError NAO-retryable com um marcador que o
    `_run_model_activity` do workflow reconhece — falha limpa, sem output
    truncado (P6). Marcado como FAKE de fronteira (WS-D/WS-C reais fazem isso
    de verdade)."""
    spec = state.fail_closed_on.get(name)
    if not spec:
        return
    times = spec.get("times", 0)
    if times <= 0:
        return
    spec["times"] = times - 1
    marker = spec.get("marker", "egress_proxy_unreachable_fail_closed")
    raise ApplicationError(marker, type="EgressFailClosed", non_retryable=True)


def _maybe_transient_fail(state: "FakeControlPlane", name: str) -> None:
    """WSB-E5-T3b — simula uma oscilacao TRANSIENTE do gateway (LiteLLM cai e
    volta mid-task). Erro RETRYABLE: o proprio Temporal retenta a Activity ate
    passar — a durabilidade absorve a oscilacao sem perder progresso nem
    truncar output (P6/P8)."""
    spec = state.transient_fail_on.get(name)
    if not spec:
        return
    times = spec.get("times", 0)
    if times <= 0:
        return
    spec["times"] = times - 1
    raise ApplicationError("gateway_oscillation_transient", type="GatewayUnavailable",
                           non_retryable=False)

from dse_contracts.activities import (
    ACTIVITY_CHECKPOINT_SANDBOX,
    ACTIVITY_CONSUME_CI_STATUS,
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
    CheckpointSandboxInput,
    CiStatusResult,
    ConsumeCiStatusInput,
    CoderTurnResult,
    DemoEvidenceResult,
    FinalizePrInput,
    L1Finding,
    L1Result,
    L2Verdict,
    MergeVerification,
    PreviewRef,
    PrRef,
    ProvisionSandboxInput,
    RebuildSandboxInput,
    RunCoderTurnInput,
    RunDemoEvidenceInput,
    RunL1PipelineInput,
    RunVisualDiffInput,
    SandboxHandle,
    TeardownSandboxInput,
    TesterTurnResult,
    TriggerPreviewInput,
    UpdateBaseBranchInput,
    UpdateBaseBranchResult,
    VerifyMergeInput,
    VisualDiffResult,
)
from dse_contracts.plan_artifact import PlanArtifact
# Todos os modelos de input agora vêm do contrato canônico. Assim esta suíte
# detecta drift no wire sem depender de importar os serviços donos.
from dse_contracts.activities import (
    RunL2ReviewInput,
    RunPlannerTurnInput,
    RunTesterTurnInput,
)


@dataclass
class FakeControlPlane:
    """Estado mutavel injetado num teste especifico para dirigir o
    comportamento das fakes (quantas vezes L1 falha, sequencia de status de
    CI, se checkpoint falha N vezes antes de funcionar, etc.)."""

    l1_fail_times: int = 0
    ci_sequence: list[str] = field(default_factory=lambda: ["green"])
    checkpoint_fail_times: int = 0
    provision_calls: int = 0
    coder_turn_calls: int = 0
    checkpoint_calls: int = 0
    rebuild_calls: int = 0
    teardown_calls: int = 0
    finalize_calls: int = 0
    # S7: mapeia work_item_id -> pr_number para o finalize ser idempotente por
    # work item (o call site nao passa mais `existing_pr_number` — o PR e
    # resolvido por work_item_id dentro do finalize real).
    pr_by_wi: dict = field(default_factory=dict)
    l1_calls: int = 0
    pr_counter: int = 1000
    calls_log: list[str] = field(default_factory=list)
    # permite ao teste "travar" uma activity ate ser liberada (usado pelo chaos test
    # para simular uma Activity longa em andamento quando o worker morre)
    coder_turn_hang_event: Any = None

    # --- Fase 2: Planner / Tester / Reviewer L2 (WS-C/WS-E fronteiras) ---
    planner_calls: int = 0
    tester_calls: int = 0
    l2_calls: int = 0
    # risco declarado pelo Planner (default low -> auto-aprova o gate)
    plan_risk_class: str = "low"
    plan_expected_files: list[str] = field(default_factory=lambda: ["app.py"])
    planner_cost_usd: float = 0.0
    tester_cost_usd: float = 0.0
    tester_tests_ran: bool = True
    tester_tests_passed: bool = True
    tester_returncode: int = 0
    l2_cost_usd: float = 0.0
    coder_cost_usd: float = 0.01
    # L2: falha N vezes (objecoes) antes de aprovar
    l2_fail_times: int = 0
    l2_objections: list[str] = field(default_factory=lambda: ["app.py:12 sem teste"])
    # captura do ULTIMO payload que a fake L2 recebeu (prova de isolamento P3)
    last_l2_payload: dict | None = None
    # simula recusa fail-closed do caminho de modelo (WSB-E5-T3b): nome da
    # activity -> {"times": N, "marker": str}. Erro NAO-retryable -> falha limpa.
    fail_closed_on: dict = field(default_factory=dict)
    # simula oscilacao TRANSIENTE do gateway (LiteLLM instavel mid-task): nome
    # da activity -> {"times": N}. Erro RETRYABLE -> Temporal retenta ate passar.
    transient_fail_on: dict = field(default_factory=dict)

    # --- Fase 3: pipeline de evidencia (fronteira WS-E) ---
    # Os fakes DECODIFICAM o payload com os models REAIS do contrato
    # (TriggerPreviewInput/RunDemoEvidenceInput/RunVisualDiffInput) — nada de
    # dict leniente (licao do adendo 02: 14 bugs de boundary nas Fases 1-2).
    trigger_preview_calls: int = 0
    demo_evidence_calls: int = 0
    visual_diff_calls: int = 0
    # files_changed que o fake Coder reporta (dirige o paths-filter do preview)
    coder_files_changed: list[str] = field(default_factory=lambda: ["app.py"])
    # "auto" = paths-filter deterministico (espelho do FR-20 do WS-E);
    # "created"/"degraded" forcam o status; "raise" derruba a Activity inteira.
    preview_mode: str = "auto"
    # §F F1 — verificação de merge via API do GitHub (fake): "verified" (default,
    # PR de fato merged), "not_merged" (forjado → refuta), "unavailable" (API
    # fora → degrada p/ envelope).
    merge_verify_mode: str = "verified"
    demo_passed: bool = True
    demo_video_key: str | None = "evidence/demo.webm"
    demo_trace_key: str | None = "evidence/trace.zip"
    visual_diff_changed_pct: float = 0.0
    last_preview_payload: dict | None = None
    last_demo_payload: dict | None = None
    last_visual_diff_payload: dict | None = None

    # --- Fase 4: merge-base / base-drift (fronteira WS-E, WSE-E6-T16) ---
    update_base_calls: int = 0
    # controla o retorno da fake: drift presente? conflito? threads orfas?
    base_has_drift: bool = True
    base_conflict: bool = False
    base_orphaned_threads: int = 0  # exit da Fase 4 exige 0 no merge-base real
    last_update_base_payload: dict | None = None
    last_l1_payload: dict | None = None
    last_finalize_payload: dict | None = None
    last_ci_payload: dict | None = None


def build_fake_activities(state: FakeControlPlane) -> list[Any]:
    async def run_planner_turn(payload: dict) -> PlanArtifact:
        state.planner_calls += 1
        state.calls_log.append("run_planner_turn")
        _maybe_fail_closed(state, ACTIVITY_RUN_PLANNER_TURN)
        RunPlannerTurnInput(**payload)  # decode REAL do contrato
        return PlanArtifact(
            work_item_id=payload["work_item_id"],
            steps=["passo 1", "passo 2"],
            expected_files=list(state.plan_expected_files),
            test_plan="cobre o caminho feliz",
            risk_class=state.plan_risk_class,
        )

    async def run_tester_turn(payload: dict) -> TesterTurnResult:
        state.tester_calls += 1
        state.calls_log.append("run_tester_turn")
        _maybe_fail_closed(state, ACTIVITY_RUN_TESTER_TURN)
        RunTesterTurnInput(**payload)  # decode REAL do contrato
        return TesterTurnResult(
            sandbox_id=payload["sandbox_id"],
            test_files=["test_app.py"],
            tests_ran=state.tester_tests_ran,
            tests_passed=state.tester_tests_passed,
            returncode=state.tester_returncode,
            cost_usd=state.tester_cost_usd,
        )

    async def run_l2_review(payload: dict) -> L2Verdict:
        state.l2_calls += 1
        state.calls_log.append("run_l2_review")
        state.last_l2_payload = dict(payload)
        _maybe_fail_closed(state, ACTIVITY_RUN_L2_REVIEW)
        RunL2ReviewInput(**payload)  # decode REAL (extra=forbid — P3 estrutural)
        if state.l2_fail_times > 0:
            state.l2_fail_times -= 1
            return L2Verdict(
                work_item_id=payload["work_item_id"], passed=False,
                objections=list(state.l2_objections), cost_usd=state.l2_cost_usd,
            )
        return L2Verdict(
            work_item_id=payload["work_item_id"], passed=True, objections=[],
            cost_usd=state.l2_cost_usd,
        )

    async def provision_sandbox(payload: dict) -> SandboxHandle:
        state.provision_calls += 1
        state.calls_log.append("provision_sandbox")
        _maybe_fail_closed(state, ACTIVITY_PROVISION_SANDBOX)
        inp = ProvisionSandboxInput(**payload)  # decode REAL
        return SandboxHandle(
            sandbox_id=f"sbx-{inp.work_item_id}",
            work_item_id=inp.work_item_id,
            tenant_id=inp.tenant_id,
            branch=f"dse/{inp.work_item_id}",
            container_id=f"ctr-{inp.work_item_id}",
        )

    async def run_coder_turn(payload: dict) -> CoderTurnResult:
        state.coder_turn_calls += 1
        state.calls_log.append("run_coder_turn")
        _maybe_transient_fail(state, ACTIVITY_RUN_CODER_TURN)
        _maybe_fail_closed(state, ACTIVITY_RUN_CODER_TURN)
        if state.coder_turn_hang_event is not None:
            await state.coder_turn_hang_event.wait()
        RunCoderTurnInput(**payload)  # decode REAL
        return CoderTurnResult(
            sandbox_id=payload["sandbox_id"],
            diff_summary="fake diff",
            files_changed=list(state.coder_files_changed),
            cost_usd=state.coder_cost_usd,
            tokens_in=10,
            tokens_out=10,
        )

    async def checkpoint_sandbox(payload: dict) -> CheckpointRef:
        state.checkpoint_calls += 1
        state.calls_log.append("checkpoint_sandbox")
        inp = CheckpointSandboxInput(**payload)  # decode REAL
        if state.checkpoint_fail_times > 0:
            state.checkpoint_fail_times -= 1
            raise RuntimeError("simulated checkpoint failure")
        git_ref = "base0001" if inp.phase == "base" else f"head{state.checkpoint_calls:04d}"
        return CheckpointRef(
            work_item_id=inp.work_item_id, git_ref=git_ref, phase=inp.phase,
            base_sha="base0001",
        )

    async def rebuild_sandbox(payload: dict) -> SandboxHandle:
        state.rebuild_calls += 1
        state.calls_log.append("rebuild_sandbox")
        inp = RebuildSandboxInput(**payload)  # decode REAL (exige checkpoint_ref)
        return SandboxHandle(
            sandbox_id=f"sbx-{inp.work_item_id}-rebuilt",
            work_item_id=inp.work_item_id,
            tenant_id=inp.tenant_id,
            branch="dse/rebuilt",
        )

    async def teardown_sandbox(payload: dict) -> None:
        state.teardown_calls += 1
        state.calls_log.append("teardown_sandbox")
        TeardownSandboxInput(**payload)  # decode REAL (exige tenant_id)

    async def run_l1_pipeline(payload: dict) -> L1Result:
        state.l1_calls += 1
        state.calls_log.append("run_l1_pipeline")
        state.last_l1_payload = dict(payload)
        inp = RunL1PipelineInput(**payload)  # decode REAL do contrato (S7)
        wi = inp.sandbox.work_item_id
        if state.l1_fail_times > 0:
            state.l1_fail_times -= 1
            return L1Result(
                work_item_id=wi,
                passed=False,
                findings=[L1Finding(check="test", passed=False, detail="simulated failure")],
            )
        return L1Result(
            work_item_id=wi,
            passed=True,
            findings=[L1Finding(check="test", passed=True)],
        )

    async def finalize_pr(payload: dict) -> PrRef:
        state.finalize_calls += 1
        state.calls_log.append("finalize_pr")
        state.last_finalize_payload = dict(payload)
        inp = FinalizePrInput(**payload)  # decode REAL (exige summary + sandbox)
        wi = inp.work_item_id
        pr_number = state.pr_by_wi.get(wi)
        if pr_number is None:
            pr_number = state.pr_counter  # usa o valor atual ANTES de incrementar
            state.pr_counter += 1
            state.pr_by_wi[wi] = pr_number
        return PrRef(
            work_item_id=wi,
            pr_number=pr_number,
            url=f"https://github.com/x/y/pull/{pr_number}",
        )

    async def verify_merge_state(payload: dict) -> MergeVerification:
        # §F F1 — espelha o contrato real; o modo dirige o veredito.
        state.calls_log.append("verify_merge_state")
        inp = VerifyMergeInput(**payload)  # decode REAL do contrato
        if state.merge_verify_mode == "not_merged":
            return MergeVerification(exists=True, merged=False, verified=False,
                                     reason="not_merged(state=open)")
        if state.merge_verify_mode == "unavailable":
            return MergeVerification(verified=False, reason="api_error:Timeout")
        return MergeVerification(exists=True, merged=True, merged_by="usr_test",
                                 merge_commit_sha="deadbeef", head_sha=inp.expected_head_sha,
                                 verified=True, reason="ok")

    # S3 (Fase 5): post_tracking_comment agora é uma Activity LOCAL REAL
    # (em LOCAL_ACTIVITIES) — não é mais fake aqui, senão colide (dois
    # @activity.defn com o mesmo nome no worker de teste). A real é
    # best-effort: sem adapter no ar nos testes, retorna ok=False sem crashar.

    async def consume_ci_status(payload: dict) -> CiStatusResult:
        state.calls_log.append("consume_ci_status")
        state.last_ci_payload = dict(payload)
        inp = ConsumeCiStatusInput(**payload)  # decode REAL (exige tenant/repo/ref)
        status = state.ci_sequence.pop(0) if state.ci_sequence else "green"
        return CiStatusResult(
            work_item_id=inp.work_item_id, pr_number=inp.pr_number, status=status
        )

    # ------------------------------------------------------------------
    # Fase 3 — pipeline de evidencia (fronteira WS-E). Cada fake decodifica o
    # payload com o MODEL REAL do contrato: se o call site do workflow deriva
    # do contrato, o teste quebra AQUI (nao no wire) — licao do adendo 02.
    # ------------------------------------------------------------------
    def _preview_kind(files: list[str]) -> str:
        # espelho do paths-filter deterministico do WS-E (FR-20 + plano 08 §D)
        # para o fake — ui (front) tem precedencia; senao serviço deployável
        # (back: fonte/Dockerfile/manifest); senao none (docs/config).
        for f in files:
            if f.startswith(("ui/", "frontend/")) or f.endswith((".css", ".tsx", ".jsx")):
                return "ui"
        for f in files:
            if (f.endswith((".py", ".go", ".rb", ".java", ".ts", ".js", "Dockerfile"))
                    or f.startswith(("k8s/", "deploy/", "charts/"))
                    or f in ("pyproject.toml", "go.mod", "package.json")):
                return "deployable"
        return "none"

    async def trigger_preview(payload: dict) -> PreviewRef:
        state.trigger_preview_calls += 1
        state.calls_log.append("trigger_preview")
        state.last_preview_payload = dict(payload)
        inp = TriggerPreviewInput(**payload)  # decode REAL do contrato
        if state.preview_mode == "raise":
            raise ApplicationError("argocd unreachable (fake)",
                                   type="PreviewProvisionError", non_retryable=True)
        # Plano 08 §D — gate deploys_preview (operator-set). Repo desabilitado
        # pula LIMPO (antes de qualquer provisionamento).
        if not inp.preview_enabled:
            return PreviewRef(work_item_id=inp.work_item_id, pr_number=inp.pr_number,
                              status="skipped_disabled", detail="repo sem preview (fake)")
        if state.preview_mode == "degraded":
            return PreviewRef(work_item_id=inp.work_item_id, pr_number=inp.pr_number,
                              status="degraded", detail="argocd sync failed (fake)")
        kind = _preview_kind(inp.files_changed)
        if state.preview_mode == "created" or kind != "none":
            return PreviewRef(
                work_item_id=inp.work_item_id, pr_number=inp.pr_number, status="created",
                namespace=f"preview-{inp.work_item_id}",
                url=f"http://preview-{inp.work_item_id}.local",
                kind=(kind if kind != "none" else "ui"),
            )
        return PreviewRef(work_item_id=inp.work_item_id, pr_number=inp.pr_number,
                          status="skipped_backend_only",
                          detail="paths-filter: sem mudança previewável (§D)")

    async def run_demo_evidence(payload: dict) -> DemoEvidenceResult:
        state.demo_evidence_calls += 1
        state.calls_log.append("run_demo_evidence")
        state.last_demo_payload = dict(payload)
        inp = RunDemoEvidenceInput(**payload)  # decode REAL do contrato
        return DemoEvidenceResult(
            work_item_id=inp.work_item_id, passed=state.demo_passed,
            video_artifact_key=state.demo_video_key,
            trace_artifact_key=state.demo_trace_key,
            duration_s=1.2, detail="fake demo (publish interno ao WS-E)",
        )

    async def run_visual_diff(payload: dict) -> VisualDiffResult:
        state.visual_diff_calls += 1
        state.calls_log.append("run_visual_diff")
        state.last_visual_diff_payload = dict(payload)
        inp = RunVisualDiffInput(**payload)  # decode REAL do contrato
        baseline_created = inp.base_screenshot_key is None
        return VisualDiffResult(
            work_item_id=inp.work_item_id,
            passed=state.visual_diff_changed_pct <= inp.threshold_pct,
            changed_pct=state.visual_diff_changed_pct,
            diff_artifact_key=f"evidence/{inp.work_item_id}/visual.png",
            baseline_created=baseline_created,
        )

    async def update_base_branch(payload: dict) -> UpdateBaseBranchResult:
        state.update_base_calls += 1
        state.calls_log.append("update_base_branch")
        state.last_update_base_payload = dict(payload)
        inp = UpdateBaseBranchInput(**payload)  # decode REAL do contrato (WSE-E6-T16)
        if state.base_conflict:
            # conflito nao-resolvivel: o workflow deve escalar (nunca resolve a forca)
            return UpdateBaseBranchResult(
                work_item_id=inp.work_item_id, strategy="merge_base",
                conflict=True, orphaned_threads=0, detail="merge conflict (fake)",
            )
        if not state.base_has_drift:
            return UpdateBaseBranchResult(
                work_item_id=inp.work_item_id, strategy="noop_no_drift",
                conflict=False, orphaned_threads=0, detail="no drift (fake)",
            )
        # P1: a estrategia e determinada pelo first_human_review_done. Depois do
        # 1o review (True) -> SEMPRE merge_base (nunca rebase). Zero orfas por
        # construcao — a menos que o teste force o cenario de violacao.
        strategy = "merge_base" if inp.first_human_review_done else "rebase_prefirst_review"
        return UpdateBaseBranchResult(
            work_item_id=inp.work_item_id, strategy=strategy, conflict=False,
            orphaned_threads=state.base_orphaned_threads, detail="fake merge-base",
        )

    return [
        activity.defn(name=ACTIVITY_UPDATE_BASE_BRANCH)(update_base_branch),
        activity.defn(name=ACTIVITY_RUN_PLANNER_TURN)(run_planner_turn),
        activity.defn(name=ACTIVITY_RUN_TESTER_TURN)(run_tester_turn),
        activity.defn(name=ACTIVITY_RUN_L2_REVIEW)(run_l2_review),
        activity.defn(name=ACTIVITY_PROVISION_SANDBOX)(provision_sandbox),
        activity.defn(name=ACTIVITY_RUN_CODER_TURN)(run_coder_turn),
        activity.defn(name=ACTIVITY_CHECKPOINT_SANDBOX)(checkpoint_sandbox),
        activity.defn(name=ACTIVITY_REBUILD_SANDBOX)(rebuild_sandbox),
        activity.defn(name=ACTIVITY_TEARDOWN_SANDBOX)(teardown_sandbox),
        activity.defn(name=ACTIVITY_RUN_L1_PIPELINE)(run_l1_pipeline),
        activity.defn(name=ACTIVITY_FINALIZE_PR)(finalize_pr),
        activity.defn(name=ACTIVITY_VERIFY_MERGE_STATE)(verify_merge_state),
        # post_tracking_comment: real, vem de LOCAL_ACTIVITIES (S3) — não registrar aqui.
        activity.defn(name=ACTIVITY_CONSUME_CI_STATUS)(consume_ci_status),
        activity.defn(name=ACTIVITY_TRIGGER_PREVIEW)(trigger_preview),
        activity.defn(name=ACTIVITY_RUN_DEMO_EVIDENCE)(run_demo_evidence),
        activity.defn(name=ACTIVITY_RUN_VISUAL_DIFF)(run_visual_diff),
    ]
