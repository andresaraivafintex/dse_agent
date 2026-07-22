"""Contrato de Activities entre WS-B (orquestrador, quem chama), WS-C (sandbox,
quem implementa as Activities de execução) e WS-E (validação/PR, quem
implementa as Activities de gate/finalize). Os tipos abaixo são o que
atravessa a fronteira Activity — os `@activity.defn` de verdade (decorados
com `temporalio.activity`) vivem no serviço dono, mas usam estes tipos como
parâmetro/retorno para que WS-B possa escrever o workflow contra uma
interface estável antes de qualquer implementação existir.

Convenção: cada activity real é registrada no Worker único de
`services/orchestrator/worker.py` (dono: WS-B), que importa o módulo de
Activities de cada workstream. Import defensivo (try/except ImportError) é
esperado enquanto os workstreams constroem em paralelo.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from .plan_artifact import PlanArtifact


# ---------------------------------------------------------------------------
# Nomes de activity (usados em `@activity.defn(name=...)` e em
# `workflow.execute_activity(name, ...)` — precisam bater nos dois lados).
# ---------------------------------------------------------------------------
ACTIVITY_PROVISION_SANDBOX = "provision_sandbox"
ACTIVITY_RUN_CODER_TURN = "run_coder_turn"
ACTIVITY_CHECKPOINT_SANDBOX = "checkpoint_sandbox"
ACTIVITY_REBUILD_SANDBOX = "rebuild_sandbox"
ACTIVITY_TEARDOWN_SANDBOX = "teardown_sandbox"
ACTIVITY_RUN_L1_PIPELINE = "run_l1_pipeline"
ACTIVITY_FINALIZE_PR = "finalize_pr"
ACTIVITY_POST_TRACKING_COMMENT = "post_tracking_comment"
ACTIVITY_CONSUME_CI_STATUS = "consume_ci_status"
ACTIVITY_EMIT_AUDIT = "emit_audit_event"

# --- Fase 2 (split de sessões stage-scoped + L2, ADR-13/FR-08/FR-13) ---
# Donos: WS-C implementa as sessões (planner/tester/reviewer L2 — a sessão L2
# é construída no WS-C por decisão de de-duplicação do plano mestre §7; o
# WS-E orquestra o loop de fix-retries em torno dela); WS-B chama por nome.
ACTIVITY_RUN_PLANNER_TURN = "run_planner_turn"
ACTIVITY_RUN_TESTER_TURN = "run_tester_turn"
ACTIVITY_RUN_L2_REVIEW = "run_l2_review"

# --- Fase 3 (pipeline de evidência, ADR-26/ADR-27/§10.12-13) ---
# Donos: WS-E implementa (evidência/preview/artifacts); WS-B chama por nome
# depois do PR finalizado. Definidos ANTES da implementação (gate de entrada
# da Fase 3, adendo 02 §2.3) — os models de input/output correspondentes
# vivem NESTE arquivo desde o dia zero, para a classe de bug de boundary das
# Fases 1-2 (14 ocorrências) não ter onde nascer.
ACTIVITY_RUN_DEMO_EVIDENCE = "run_demo_evidence"
ACTIVITY_PUBLISH_ARTIFACT = "publish_artifact"
ACTIVITY_TRIGGER_PREVIEW = "trigger_preview"
ACTIVITY_RUN_VISUAL_DIFF = "run_visual_diff"

# --- Fase 4 (loop hardening & learning) ---
# Definidos ANTES da implementação (gate de entrada §4 do adendo 03, mesma
# disciplina da Fase 3). Donos: merge-base = WS-E; promoção de skill = WS-C
# (a esteira eval->approval->canary->rollback); WS-B chama por nome.
ACTIVITY_UPDATE_BASE_BRANCH = "update_base_branch"   # merge-base, nunca rebase (WSE-E6-T16)
ACTIVITY_EVAL_SKILL_CANDIDATE = "eval_skill_candidate"  # replay contra eval set (WSC-E4-T3)
ACTIVITY_PROMOTE_SKILL = "promote_skill"             # transição de estado governada (WSC-E4-T3)


# ---------------------------------------------------------------------------
# Dono: WS-C (services/sandbox-runtime)
# ---------------------------------------------------------------------------
class SandboxHandle(BaseModel):
    sandbox_id: str
    work_item_id: str
    tenant_id: str
    branch: str
    container_id: str | None = None  # id do container Docker por trás do handle
    # Aditivos: runtimes novos retornam os SHAs que delimitam a sessao. Os
    # defaults preservam handles ja gravados em histories do Temporal.
    base_sha: str | None = None
    head_sha: str | None = None


class CheckpointRef(BaseModel):
    work_item_id: str
    git_ref: str  # commit sha no branch da tarefa
    phase: str  # nome da fronteira de fase em que o checkpoint foi tirado
    base_sha: str | None = None


class CoderTurnResult(BaseModel):
    sandbox_id: str
    diff_summary: str
    files_changed: list[str]
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    base_sha: str | None = None
    head_sha: str | None = None


# ---------------------------------------------------------------------------
# Inputs canonicos das fronteiras sandbox/validacao. Historicamente alguns
# destes modelos viviam apenas no servico dono; isso permitia que os fakes do
# workflow aceitassem dicts diferentes do wire real. Eles passam a morar no
# pacote de contratos, com defaults aditivos para histories ja persistidos.
# O servico dono pode resolver campos vazios server-side pelo work_item_id.
# ---------------------------------------------------------------------------
class ProvisionSandboxInput(BaseModel):
    work_item_id: str
    tenant_id: str
    branch: str | None = None
    base_branch: str = "main"
    repo: str | None = None
    budget: dict[str, Any] = Field(default_factory=dict)
    image: str | None = None
    base_sha: str | None = None


class CheckpointSandboxInput(BaseModel):
    work_item_id: str
    tenant_id: str
    sandbox_id: str | None = None
    branch: str | None = None
    phase: str = "manual"
    base_sha: str | None = None


class RebuildSandboxInput(BaseModel):
    work_item_id: str
    tenant_id: str
    checkpoint_ref: CheckpointRef
    branch: str | None = None
    budget: dict[str, Any] = Field(default_factory=dict)
    image: str | None = None
    base_sha: str | None = None


class TeardownSandboxInput(BaseModel):
    work_item_id: str
    tenant_id: str
    stage: str = "coder"


class RunCoderTurnInput(BaseModel):
    work_item_id: str
    tenant_id: str
    instruction: str
    sandbox_id: str | None = None
    branch: str | None = None
    stage: str = "coder"
    task_class: str = "default"
    data_class: str = "internal"
    model_override: str | None = None
    runtime_override: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None


class RunL1PipelineInput(BaseModel):
    """Input tolerante ao payload antigo.

    ``sandbox``/``plan`` eram ausentes em histories antigos; o owner resolve
    essas lacunas server-side usando ``work_item_id``. Novos callers sempre
    enviam base/head SHA e o L1 calcula o diff pelo SHA imutavel.
    """

    work_item_id: str = ""
    sandbox_id: str | None = None
    sandbox: SandboxHandle | None = None
    plan: PlanArtifact | None = None
    tenant_id: str = ""
    base_branch: str = "main"
    base_sha: str = ""
    head_sha: str = ""
    target_dir: str = "."
    repo_dir: str = "/workspace/repo"

    @model_validator(mode="after")
    def _derive_work_item_id(self) -> "RunL1PipelineInput":
        if not self.work_item_id and self.sandbox is not None:
            self.work_item_id = self.sandbox.work_item_id
        if not self.sandbox_id and self.sandbox is not None:
            self.sandbox_id = self.sandbox.sandbox_id
        if not self.base_sha and self.sandbox is not None and self.sandbox.base_sha:
            self.base_sha = self.sandbox.base_sha
        if not self.head_sha and self.sandbox is not None and self.sandbox.head_sha:
            self.head_sha = self.sandbox.head_sha
        return self


class FinalizePrInput(BaseModel):
    """Finalize aditivo; vazios sao resolvidos pelo owner a partir do SoR."""

    work_item_id: str
    tenant_id: str = ""
    repo: str = ""
    branch: str = ""
    base_branch: str = "main"
    base_sha: str = ""
    head_sha: str = ""
    summary: str = ""
    risk_class: str = "low"
    evidence_url: str = ""
    issue_ref: dict[str, Any] | None = None
    sandbox_id: str | None = None
    sandbox: SandboxHandle | None = None
    repo_dir: str = "/workspace/repo"
    strict_mode: bool | None = None
    surface_ref: dict[str, Any] | None = None


class ConsumeCiStatusInput(BaseModel):
    """Campos novos possuem default para auto-cura de payload historico."""

    work_item_id: str
    tenant_id: str = ""
    repo: str = ""
    pr_number: int
    ref: str = ""
    base_sha: str = ""
    head_sha: str = ""
    surface_ref: dict[str, Any] | None = None


class PersistWorkItemStateInput(BaseModel):
    """Projecao idempotente do workflow no Postgres.

    Somente ``work_item_id`` e obrigatorio para que payloads antigos da
    Activity continuem decodificando. Campos ausentes sao preservados pelo
    servidor; plano/hash/expected_files sao derivados server-side.
    """

    work_item_id: str
    status: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    plan: dict[str, Any] | None = None
    risk_class: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    ci_status: str | None = None
    last_error: str | None = None
    clear_ci_status: bool = False
    validation_attempts: dict[str, int] | None = None


# ---------------------------------------------------------------------------
# Dono: WS-E (services/validation)
# ---------------------------------------------------------------------------
class GateStatus(str, Enum):
    """Resultado estruturado comum a L1/L2/L3.

    Apenas PASS autoriza avancar. ``passed`` continua existindo nos modelos
    para compatibilidade com produtores/consumidores antigos.
    """

    PASS = "PASS"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"
    NOT_CONFIGURED = "NOT_CONFIGURED"


class L1Finding(BaseModel):
    check: str  # "lint" | "typecheck" | "test" | "build" | "sast" | "secret_scan" | "diff_budget" | "forbidden_paths"
    passed: bool = False
    status: GateStatus | None = None
    detail: str = ""

    @model_validator(mode="after")
    def _normalize_status(self) -> "L1Finding":
        if self.status is None:
            self.status = GateStatus.PASS if self.passed else GateStatus.FAIL
        elif self.passed != (self.status == GateStatus.PASS):
            raise ValueError("passed deve ser true somente quando status=PASS")
        return self


class L1Result(BaseModel):
    work_item_id: str
    passed: bool = False
    status: GateStatus | None = None
    findings: list[L1Finding]
    base_sha: str | None = None
    head_sha: str | None = None

    @model_validator(mode="after")
    def _normalize_status(self) -> "L1Result":
        if self.status is None:
            if self.passed:
                self.status = GateStatus.PASS
            else:
                statuses = {f.status for f in self.findings}
                for candidate in (
                    GateStatus.ERROR,
                    GateStatus.NOT_CONFIGURED,
                    GateStatus.FAIL,
                    GateStatus.SKIPPED,
                ):
                    if candidate in statuses:
                        self.status = candidate
                        break
                else:
                    self.status = GateStatus.FAIL
        elif self.passed != (self.status == GateStatus.PASS):
            raise ValueError("passed deve ser true somente quando status=PASS")
        return self


class PrRef(BaseModel):
    # Fase 2 (adendo 01 §4, aprovado pelo arquiteto): `pr_number` opcional +
    # `compare_url` para o modo estrito (WSE-E3-T8) em que o sistema só faz
    # push do branch e posta um compare link — o PR é aberto por um humano.
    # Mudança aditiva: todo caller da Fase 1 continua construindo com
    # pr_number preenchido; exatamente um dos dois deve estar presente.
    work_item_id: str
    pr_number: int | None = None
    url: str
    compare_url: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None


class L2Verdict(BaseModel):
    """Veredito estruturado da sessão Reviewer de contexto fresco (Fase 2,
    WSC-E3-T5 constrói a sessão / WSE-E2 orquestra o loop). P3: a sessão L2
    recebe APENAS plan artifact + diff final — nunca o histórico do Coder."""

    work_item_id: str
    passed: bool = False
    status: GateStatus | None = None
    objections: list[str] = []  # vazia quando passed; específicas (arquivo/linha) quando não
    cost_usd: float = 0.0
    base_sha: str | None = None
    head_sha: str | None = None

    @model_validator(mode="after")
    def _normalize_status(self) -> "L2Verdict":
        if self.status is None:
            self.status = GateStatus.PASS if self.passed else GateStatus.FAIL
        elif self.passed != (self.status == GateStatus.PASS):
            raise ValueError("passed deve ser true somente quando status=PASS")
        return self


class CiStatusResult(BaseModel):
    work_item_id: str
    pr_number: int
    status: str  # "pending" | "green" | "red"
    head_sha: str | None = None


# ---------------------------------------------------------------------------
# Models de sessão promovidos à fundação (adendo 02 §2.3 — Fase 3, gate de
# entrada). Nas Fases 1-2 estes models viviam em `sandbox_runtime.activities`
# e o payload do chamador (WS-B) derivou dos campos declarados sem que nenhum
# teste pegasse (fakes lenientes dos dois lados) — 14 bugs de boundary no
# total. A partir daqui: UMA fonte da verdade, com testes de regressão de
# boundary em packages/contracts/tests/test_activity_boundaries.py que
# validam os payloads EXATOS que o workflow envia. `sandbox_runtime` importa
# daqui e re-exporta para compatibilidade.
# ---------------------------------------------------------------------------
class RunPlannerTurnInput(BaseModel):
    """Input da sessão Planner (WSC-E3-T3). Tolerante por design aos aliases
    que o workflow do WS-B envia (`instructions` lista / `base_branch`) —
    reconciliação da integração da Fase 2, agora parte do contrato."""

    model_config = {"populate_by_name": True}

    work_item_id: str
    tenant_id: str
    instruction: str = ""
    instructions: list[str] = Field(default_factory=list)
    repo: str = "app"
    branch: str | None = None
    base_branch: str | None = None
    base_sha: str | None = None
    task_class: str = "default"
    data_class: str = "internal"
    diff_budget_lines: int = 400
    related_tickets: list[str] = Field(default_factory=list)
    model_override: str | None = None  # tolerado; a política de modelo vive no gateway

    @model_validator(mode="after")
    def _reconcile(self) -> "RunPlannerTurnInput":
        if not self.instruction and self.instructions:
            self.instruction = " ".join(s for s in self.instructions if s)
        if self.branch is None and self.base_branch is not None:
            self.branch = self.base_branch
        return self


class RunTesterTurnInput(BaseModel):
    """Input da sessão Tester (WSC-E3-T4). Tolerante aos aliases do WS-B
    (`plan` dict + `sandbox_id`); deriva `instruction` do test_plan."""

    model_config = {"populate_by_name": True}

    work_item_id: str
    tenant_id: str
    instruction: str = ""
    plan: dict[str, Any] | None = None
    sandbox_id: str | None = None
    repo: str = "app"
    branch: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    task_class: str = "default"
    data_class: str = "internal"
    run_paths: list[str] = Field(default_factory=list)
    model_override: str | None = None
    runtime_override: str | None = None

    @model_validator(mode="after")
    def _reconcile(self) -> "RunTesterTurnInput":
        if not self.instruction and self.plan:
            self.instruction = str(self.plan.get("test_plan") or "write/adjust tests for the change")
        return self


class TesterTurnResult(BaseModel):
    """Retorno da sessão Tester. Superset compatível com `CoderTurnResult`
    (o workflow decodifica o retorno como CoderTurnResult — `files_changed`
    espelha `test_files`)."""

    sandbox_id: str
    test_files: list[str]
    tests_ran: bool
    tests_passed: bool
    returncode: int
    status: GateStatus | None = None
    cost_usd: float = 0.0
    diff_summary: str = ""
    files_changed: list[str] = Field(default_factory=list)
    base_sha: str | None = None
    head_sha: str | None = None

    @model_validator(mode="after")
    def _mirror_test_files(self) -> "TesterTurnResult":
        if not self.files_changed:
            self.files_changed = list(self.test_files)
        if not self.diff_summary:
            self.diff_summary = f"tester: {len(self.test_files)} test file(s)"
        if self.status is None:
            if not self.tests_ran:
                self.status = GateStatus.NOT_CONFIGURED
            elif self.tests_passed and self.returncode == 0:
                self.status = GateStatus.PASS
            else:
                self.status = GateStatus.FAIL
        return self


class RunL2ReviewInput(BaseModel):
    """Input da sessão Reviewer L2 (WSC-E3-T5). P3 ESTRUTURAL, endurecido na
    promoção ao contrato: `extra="forbid"` — qualquer tentativa de passar um
    campo além dos declarados (ex.: histórico/instruções do Coder) falha no
    DECODE da Activity, não apenas em teste. Os campos são exatamente
    {work_item_id, tenant_id, plan, diff, task_class, data_class}; quem se
    adapta é sempre o CHAMADOR (o WS-B envia `diff`, nunca `diff_summary`)."""

    model_config = {"extra": "forbid"}

    work_item_id: str
    tenant_id: str
    plan: PlanArtifact
    diff: str
    task_class: str = "default"
    data_class: str = "internal"
    base_sha: str | None = None
    head_sha: str | None = None


# ---------------------------------------------------------------------------
# Fase 3 — pipeline de evidência (dono: WS-E; chamador: WS-B). Definidos no
# contrato ANTES de qualquer implementação existir (gate de entrada).
# ---------------------------------------------------------------------------
class RunDemoEvidenceInput(BaseModel):
    """Executa o(s) teste(s) `@demo` da tarefa (convenção `demos/<work_item_id>/`,
    ADR-27) com gravação de vídeo. O teste é autorado pelo Tester (WSC-E3-T4b);
    a EXECUÇÃO aqui é determinística — Playwright real, sem LLM."""

    work_item_id: str
    tenant_id: str
    sandbox: SandboxHandle | None = None
    demo_dir: str = ""  # default derivado: demos/<work_item_id>/
    base_url: str | None = None  # URL do preview (TriggerPreview) quando houver; fixture local senão
    timeout_s: int = 120
    head_sha: str | None = None


class DemoEvidenceResult(BaseModel):
    work_item_id: str
    passed: bool
    status: GateStatus | None = None
    video_artifact_key: str | None = None  # chave no artifact store (publicado via PublishArtifact)
    trace_artifact_key: str | None = None
    duration_s: float = 0.0
    detail: str = ""
    head_sha: str | None = None

    @model_validator(mode="after")
    def _normalize_status(self) -> "DemoEvidenceResult":
        if self.status is None:
            self.status = GateStatus.PASS if self.passed else GateStatus.FAIL
        elif self.passed != (self.status == GateStatus.PASS):
            raise ValueError("passed deve ser true somente quando status=PASS")
        return self


class PublishArtifactInput(BaseModel):
    work_item_id: str
    tenant_id: str
    kind: str  # "demo_video" | "playwright_trace" | "visual_diff" | "test_report"
    local_path: str
    content_type: str = "application/octet-stream"
    ttl_seconds: int = 7 * 24 * 3600  # presigned URL de TTL curto por política


class ArtifactRef(BaseModel):
    work_item_id: str
    tenant_id: str
    kind: str
    store_key: str  # chave S3 no Garage, prefixada por tenant (NFR-03)
    presigned_url: str
    expires_at: str  # ISO-8601 — links de evidência EXPIRAM por política (exit da Fase 3)


class TriggerPreviewInput(BaseModel):
    """Dispara (ou pula) o preview environment por PR. A decisão é DETERMINÍSTICA
    por paths-filter (FR-20) — nunca por modelo:

    - `preview_enabled` (plano 08 §D) — gate operator-set (`repo_bindings.
      deploys_preview`): repo não marcado como "gera preview" pula com
      `skipped_disabled`. Default True mantém o comportamento anterior para
      callers que não passam o gate (single-repo/sem config).
    - `ui_path_globs` + `deployable_globs` — o preview vale se a mudança toca UI
      (front) OU um serviço deployável (back). Mudança só de docs/teste →
      `skipped_backend_only` (conta como sucesso, NUNCA bloqueia)."""

    work_item_id: str
    tenant_id: str
    repo: str
    pr_number: int
    files_changed: list[str] = Field(default_factory=list)
    head_sha: str | None = None
    preview_enabled: bool = True
    ui_path_globs: list[str] = Field(default_factory=lambda: ["ui/**", "frontend/**", "**/*.css", "**/*.tsx", "**/*.jsx"])
    # plano 08 §D — serviço deployável (back): fonte/manifest/container que
    # alteram o artefato servido no preview. Docs/teste-só não casam e pulam.
    deployable_globs: list[str] = Field(default_factory=lambda: [
        "**/Dockerfile", "Dockerfile", "**/*.py", "**/*.go", "**/*.rb",
        "**/*.java", "**/*.ts", "**/*.js", "k8s/**", "deploy/**", "charts/**",
        "**/requirements*.txt", "pyproject.toml", "go.mod", "package.json",
    ])


class PreviewRef(BaseModel):
    work_item_id: str
    pr_number: int
    status: str  # "created" | "skipped_backend_only" | "skipped_disabled" | "degraded"
    namespace: str | None = None
    url: str | None = None
    detail: str = ""
    # plano 08 §D — "ui" | "deployable" | "" — qual filtro disparou o preview
    # (evidência no PR/console de por que este PR ganhou ou não um preview).
    kind: str = ""


class RunVisualDiffInput(BaseModel):
    work_item_id: str
    tenant_id: str
    base_screenshot_key: str | None = None  # artifact store; None = primeiro run (baseline)
    candidate_screenshot_path: str = ""
    threshold_pct: float = 0.1


class VisualDiffResult(BaseModel):
    work_item_id: str
    passed: bool
    changed_pct: float = 0.0
    diff_artifact_key: str | None = None
    baseline_created: bool = False
    # Aditivo (pedido do WS-E na integração da Fase 3): a chave da baseline no
    # artifact store, para o chamador persistir e reenviar como
    # `base_screenshot_key` nos runs seguintes — antes disso a chave voltava
    # sobrecarregada em `diff_artifact_key` quando baseline_created=True.
    baseline_artifact_key: str | None = None


# ---------------------------------------------------------------------------
# Fase 4 — merge-base / base-drift (dono WS-E, WSE-E6-T16) e promoção de skill
# (dono WS-C, WSC-E4-T3). Contratos definidos no gate de entrada, antes do
# build. NOTA DE VALIDAÇÃO (adendo 03): merge-base é construção NOVA — a Fase 1
# nunca implementou o tratamento de drift, apesar do texto do plano.
# ---------------------------------------------------------------------------
class UpdateBaseBranchInput(BaseModel):
    """Atualiza o branch da tarefa com o drift da base SEM reescrever história.
    P1: a estratégia é determinística (código, não modelo). `first_human_review`
    distingue o único momento em que rebase é permitido (antes do 1º review):
    depois dele, SÓ merge-base-into-branch — rebase+force-push órfã as threads
    de review ancoradas em commits reescritos (comportamento verificado do
    GitHub, failure mode 11)."""

    work_item_id: str
    tenant_id: str
    repo: str
    branch: str
    base_branch: str
    first_human_review_done: bool = True  # default seguro: assume que já houve review → nunca rebase


class UpdateBaseBranchResult(BaseModel):
    work_item_id: str
    strategy: str          # "merge_base" | "rebase_prefirst_review" | "noop_no_drift"
    conflict: bool = False  # conflito não-resolvível → workflow escala a humano (nunca resolve à força)
    orphaned_threads: int = 0  # asserção de exit da Fase 4: DEVE ser 0
    detail: str = ""


class EvalSkillCandidateInput(BaseModel):
    """Replay de um skill candidate contra o eval set histórico (positivos e
    negativos) — WSC-E4-T3. Determinístico; produz um score, não uma decisão
    (a aprovação é humana)."""

    tenant_id: str
    skill_key: str
    candidate_version: int


class EvalSkillCandidateResult(BaseModel):
    skill_key: str
    candidate_version: int
    passed: bool
    score: float = 0.0
    positive_hits: int = 0
    negative_regressions: int = 0  # >0 bloqueia promoção por construção
    detail: str = ""


class PromoteSkillInput(BaseModel):
    """Transição de estado GOVERNADA da esteira de promoção (WSC-E4-T3):
    candidate → (eval) → approved → canary → active, e o rollback active/canary
    → rolled_back. P1/P3: nenhuma transição para `approved`/`active` sem
    `approver` humano resolvido — promoção sem aprovação é impossível por
    construção (a Activity recusa `to_status in {approved,active}` com
    approver vazio)."""

    tenant_id: str
    skill_key: str
    version: int
    to_status: str      # 'approved' | 'canary' | 'active' | 'rolled_back'
    approver: str | None = None  # principal resolvido; obrigatório p/ approved/active
    reason: str = ""


class PromoteSkillResult(BaseModel):
    skill_key: str
    version: int
    from_status: str
    to_status: str
    ok: bool
    detail: str = ""
