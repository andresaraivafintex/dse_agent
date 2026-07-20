"""Temporal Activities do lifecycle do sandbox (WSC-E1-T3) + sessão Coder
(WSC-E3-T2). Nomes exatos de `dse_contracts.activities` — importado pelo
worker único do WS-B (`services/orchestrator/worker.py`).

Import defensivo: este módulo em si nunca deve falhar ao ser importado só
por dependência pesada ausente no venv de quem importa — mas como
`docker`/`temporalio`/`dse_contracts`/`dse_audit` são dependências
DECLARADAS deste pacote (pyproject.toml), aqui dentro fazemos import direto
normalmente. Quem quer importar este módulo sem ter essas dependências
instaladas deve fazer isso no PRÓPRIO try/except (responsabilidade do
integrador, ver docstring de `sandbox_runtime/__init__.py`).

Estado entre chamadas de Activity: Temporal não garante que a mesma Activity
de um workflow rode sempre no mesmo worker/processo — por isso este módulo
NUNCA guarda estado em memória de processo entre chamadas. Todo estado vive:
  - no Docker (o container do sandbox, achado por label `dse.work_item_id`);
  - no filesystem, em paths determinísticos derivados de `work_item_id`
    (`_paths_for`) — workspace de trabalho + bare repo de checkpoint.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator
from temporalio import activity

from dse_audit import emit as audit_emit
from dse_contracts import (
    ACTIVITY_CHECKPOINT_SANDBOX,
    ACTIVITY_PROVISION_SANDBOX,
    ACTIVITY_REBUILD_SANDBOX,
    ACTIVITY_RUN_CODER_TURN,
    ACTIVITY_RUN_L2_REVIEW,
    ACTIVITY_RUN_PLANNER_TURN,
    ACTIVITY_RUN_TESTER_TURN,
    ACTIVITY_TEARDOWN_SANDBOX,
    CheckpointRef,
    CoderTurnResult,
    GatewayCallHeaders,
    L2Verdict,
    PlanArtifact,
    SandboxHandle,
    Stage,
)

from . import docker_driver, git_checkpoint, leases_store, metrics
from .model_gateway_client import mint_virtual_key
from .retrieval import RetrievalService
from .scoped_git import GitScopeViolation, ScopedGitSession
from .sessions import (
    FreshReviewerSession,
    PlannerContext,
    ReviewerContext,
    ScriptedAgentSession,
    classify_risk_class,
    hydrate_planner_context,
)
from .substrate import AgentSubstrate, FakeSubstrate
from .toolsets import PlannerToolset, TesterToolset

_STATE_DIR = os.environ.get("DSE_SANDBOX_STATE_DIR", "/tmp/dse-sandboxes")


def _paths_for(work_item_id: str) -> tuple[str, str]:
    """Paths determinísticos derivados só do work_item_id — permite que
    qualquer worker, em qualquer chamada, ache o mesmo workspace/bare repo
    sem depender de estado em memória (ver docstring do módulo)."""
    root = Path(_STATE_DIR) / work_item_id
    workspace_dir = str(root / "workspace")
    bare_repo_path = str(root / "checkpoint.git")
    return workspace_dir, bare_repo_path


def _default_branch(work_item_id: str) -> str:
    return f"dse/{work_item_id}"


# ---------------------------------------------------------------------------
# provision_sandbox
# ---------------------------------------------------------------------------
class ProvisionSandboxInput(BaseModel):
    work_item_id: str
    tenant_id: str
    branch: str | None = None
    base_branch: str = "main"
    budget: dict[str, Any] = Field(default_factory=dict)
    image: str | None = None


@activity.defn(name=ACTIVITY_PROVISION_SANDBOX)
async def provision_sandbox(inp: ProvisionSandboxInput) -> SandboxHandle:
    branch = inp.branch or _default_branch(inp.work_item_id)
    workspace_dir, bare_repo_path = _paths_for(inp.work_item_id)

    is_new_checkpoint_repo = not Path(bare_repo_path).exists()
    if is_new_checkpoint_repo:
        git_checkpoint.provision_checkpoint_repo(bare_repo_path, branch)
    if not Path(workspace_dir).exists():
        git_checkpoint.init_task_workspace(workspace_dir, bare_repo_path, branch, inp.base_branch)

    provisioned = docker_driver.provision_container(
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        branch=branch,
        workspace_host_path=workspace_dir,
        checkpoint_bare_repo_path=bare_repo_path,
        budget=inp.budget,
        image=inp.image or docker_driver.DEFAULT_SANDBOX_IMAGE,
    )

    audit_emit(
        actor="system:sandbox-runtime",
        action="sandbox_provisioned",
        tenant_id=inp.tenant_id,
        work_item_id=inp.work_item_id,
        details={
            "container_id": provisioned.container_id,
            "reused_existing": not provisioned.created_new,
            "resource_class": provisioned.resource_caps.resource_class,
            "branch": branch,
        },
    )
    leases_store.record_lifecycle_event(
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        container_id=provisioned.container_id,
        branch=branch,
        resource_class=provisioned.resource_caps.resource_class,
        status="provisioned",
    )

    return SandboxHandle(
        sandbox_id=provisioned.container_name,
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        branch=branch,
        container_id=provisioned.container_id,
    )


# ---------------------------------------------------------------------------
# checkpoint_sandbox
# ---------------------------------------------------------------------------
class CheckpointSandboxInput(BaseModel):
    work_item_id: str
    tenant_id: str
    branch: str | None = None
    phase: str = "manual"


@activity.defn(name=ACTIVITY_CHECKPOINT_SANDBOX)
async def checkpoint_sandbox(inp: CheckpointSandboxInput) -> CheckpointRef:
    branch = inp.branch or _default_branch(inp.work_item_id)
    workspace_dir, _bare_repo_path = _paths_for(inp.work_item_id)
    ref = git_checkpoint.checkpoint(inp.work_item_id, workspace_dir, branch, inp.phase)

    audit_emit(
        actor="system:sandbox-runtime",
        action="sandbox_checkpointed",
        tenant_id=inp.tenant_id,
        work_item_id=inp.work_item_id,
        details={"git_ref": ref.git_ref, "phase": ref.phase},
    )
    existing = docker_driver.find_existing_container(inp.work_item_id)
    leases_store.record_lifecycle_event(
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        container_id=existing.id if existing else None,
        branch=branch,
        resource_class=(existing.labels.get("dse.resource_class", "small") if existing else "small"),
        status="checkpointed",
    )
    return ref


# ---------------------------------------------------------------------------
# rebuild_sandbox
# ---------------------------------------------------------------------------
class RebuildSandboxInput(BaseModel):
    work_item_id: str
    tenant_id: str
    checkpoint_ref: CheckpointRef
    branch: str | None = None
    budget: dict[str, Any] = Field(default_factory=dict)
    image: str | None = None


@activity.defn(name=ACTIVITY_REBUILD_SANDBOX)
async def rebuild_sandbox(inp: RebuildSandboxInput) -> SandboxHandle:
    branch = inp.branch or _default_branch(inp.work_item_id)
    old_workspace_dir, bare_repo_path = _paths_for(inp.work_item_id)

    # Container antigo pode estar morto (chaos) — remove se ainda existir
    # antes de recriar, para não colidir com o nome/labels do novo.
    existing = docker_driver.find_existing_container(inp.work_item_id)
    if existing is not None:
        try:
            existing.remove(force=True)
        except Exception:  # noqa: BLE001 - já pode ter sido removido pelo daemon
            pass

    # Workspace novo (simula perda do container antigo — não reaproveita o
    # diretório de trabalho anterior, só o bare repo de checkpoint, que é a
    # fonte de verdade durável).
    rebuilt_workspace_dir = old_workspace_dir + "-rebuilt"
    recovered_sha = git_checkpoint.rebuild_from_checkpoint(
        rebuilt_workspace_dir, bare_repo_path, branch, inp.checkpoint_ref
    )

    provisioned = docker_driver.provision_container(
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        branch=branch,
        workspace_host_path=rebuilt_workspace_dir,
        checkpoint_bare_repo_path=bare_repo_path,
        budget=inp.budget,
        image=inp.image or docker_driver.DEFAULT_SANDBOX_IMAGE,
    )

    audit_emit(
        actor="system:sandbox-runtime",
        action="sandbox_rebuilt",
        tenant_id=inp.tenant_id,
        work_item_id=inp.work_item_id,
        details={
            "container_id": provisioned.container_id,
            "checkpoint_git_ref": inp.checkpoint_ref.git_ref,
            "recovered_sha": recovered_sha,
        },
    )
    leases_store.record_lifecycle_event(
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        container_id=provisioned.container_id,
        branch=branch,
        resource_class=provisioned.resource_caps.resource_class,
        status="rebuilt",
    )

    return SandboxHandle(
        sandbox_id=provisioned.container_name,
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        branch=branch,
        container_id=provisioned.container_id,
    )


# ---------------------------------------------------------------------------
# teardown_sandbox
# ---------------------------------------------------------------------------
class TeardownSandboxInput(BaseModel):
    work_item_id: str
    tenant_id: str
    stage: str = "coder"


@activity.defn(name=ACTIVITY_TEARDOWN_SANDBOX)
async def teardown_sandbox(inp: TeardownSandboxInput) -> None:
    existing = docker_driver.find_existing_container(inp.work_item_id)
    resource_class = "small"
    runtime_minutes = 0.0
    if existing is not None:
        resource_class = existing.labels.get("dse.resource_class", "small")
        runtime_minutes = docker_driver.teardown_container(existing.id)

    metrics.record_sandbox_runtime_minutes(
        tenant_id=inp.tenant_id,
        work_item_id=inp.work_item_id,
        stage=inp.stage,
        resource_class=resource_class,
        minutes=runtime_minutes,
    )
    audit_emit(
        actor="system:sandbox-runtime",
        action="sandbox_torn_down",
        tenant_id=inp.tenant_id,
        work_item_id=inp.work_item_id,
        details={"runtime_minutes": round(runtime_minutes, 4), "resource_class": resource_class},
    )
    leases_store.record_lifecycle_event(
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        container_id=existing.id if existing else None,
        branch=_default_branch(inp.work_item_id),
        resource_class=resource_class,
        status="torn_down",
    )


# ---------------------------------------------------------------------------
# run_coder_turn
# ---------------------------------------------------------------------------
class RunCoderTurnInput(BaseModel):
    work_item_id: str
    tenant_id: str
    instruction: str
    branch: str | None = None
    stage: str = "coder"
    task_class: str = "default"
    data_class: str = "internal"
    checkpoint_phase: str = "coder_turn"


def _build_substrate(script: list[dict[str, Any]] | None) -> AgentSubstrate:
    """Fábrica de substrato. Fase 1 P0: `FakeSubstrate` por padrão (nenhuma
    dependência de model-gateway/OpenHands real precisa estar de pé para os
    testes rodarem). Produção troca isto por `OpenHandsSubstrate()` — ver
    `substrate.py`."""
    return FakeSubstrate(script or [])


@activity.defn(name=ACTIVITY_RUN_CODER_TURN)
async def run_coder_turn(inp: RunCoderTurnInput) -> CoderTurnResult:
    """Wrapper fino registrado como Activity de verdade — Temporal não aceita
    argumentos extras (nem keyword-only, nem posicionais opcionais) em
    funções decoradas com `@activity.defn`. A lógica real e os pontos de
    injeção de dependência para teste (`substrate`/`script`) vivem em
    `_run_coder_turn_impl`, chamada tanto por aqui (produção, sem overrides)
    quanto diretamente pelos testes (com `FakeSubstrate` roteirizado)."""
    return await _run_coder_turn_impl(inp)


async def _run_coder_turn_impl(
    inp: RunCoderTurnInput, substrate: AgentSubstrate | None = None, script: list[dict[str, Any]] | None = None
) -> CoderTurnResult:
    """Executa um turno do Coder dentro do sandbox já provisionado.

    P1 (nenhuma decisão de fluxo por LLM): o `substrate` SÓ edita arquivos —
    o commit/push para o branch da tarefa é feito aqui, por código
    determinístico (`ScopedGitSession`), nunca pelo LLM. `substrate`/`script`
    são parâmetros de injeção de dependência usados pelos testes; em
    produção o worker do WS-B chama a Activity `run_coder_turn` sem eles e
    recebe o `FakeSubstrate` (documentar override real via env
    `DSE_CODER_SUBSTRATE=openhands` — ver README) até a integração completa
    com OpenHands.
    """
    branch = inp.branch or _default_branch(inp.work_item_id)
    workspace_dir, _bare_repo_path = _paths_for(inp.work_item_id)

    headers = GatewayCallHeaders(
        tenant_id=inp.tenant_id,
        work_item_id=inp.work_item_id,
        stage=Stage(inp.stage),
        task_class=inp.task_class,
        data_class=inp.data_class,
    )
    vk = mint_virtual_key(headers)

    agent = substrate if substrate is not None else _build_substrate(script)
    agent.create_session(
        work_item_id=inp.work_item_id,
        workspace_dir=workspace_dir,
        gateway_headers=headers,
        virtual_key=vk.virtual_key,
        gateway_base_url=vk.gateway_base_url,
    )

    base_sha_session = ScopedGitSession(workspace_dir=workspace_dir, branch=branch)
    base_sha = base_sha_session.current_sha()

    done = False
    max_turns = 8
    turns = 0
    while not done and turns < max_turns:
        log = agent.run_turn(inp.instruction)
        done = log.done
        turns += 1

    artifacts = agent.collect_artifacts()

    # Commit/push determinístico — o substrato nunca tem acesso a git.
    git_session = ScopedGitSession(workspace_dir=workspace_dir, branch=branch)
    git_session.ensure_identity()
    if git_session.has_changes():
        git_session.commit(f"coder({inp.work_item_id}): {inp.instruction[:72]}")
    try:
        git_session.push()
    except GitScopeViolation:
        audit_emit(
            actor="system:sandbox-runtime",
            action="coder_push_rejected",
            tenant_id=inp.tenant_id,
            work_item_id=inp.work_item_id,
            details={"branch": branch},
        )
        raise

    files_changed = git_session.files_changed_against(base_sha) if base_sha != git_session.current_sha() else []

    audit_emit(
        actor="system:sandbox-runtime",
        action="coder_turn_completed",
        tenant_id=inp.tenant_id,
        work_item_id=inp.work_item_id,
        details={
            "instruction": inp.instruction,
            "files_changed": files_changed or artifacts.files_changed,
            "cost_usd": artifacts.cost_usd,
            "virtual_key_fixture": vk.fixture,
        },
    )

    return CoderTurnResult(
        sandbox_id=artifacts.sandbox_id,
        diff_summary=artifacts.diff_summary,
        files_changed=files_changed or artifacts.files_changed,
        cost_usd=artifacts.cost_usd,
        tokens_in=artifacts.tokens_in,
        tokens_out=artifacts.tokens_out,
    )


# ===========================================================================
# Fase 2 — sessões stage-scoped (WSC-E3-T3/T4/T5)
# ===========================================================================

# ---------------------------------------------------------------------------
# run_planner_turn (WSC-E3-T3) — sessão read-only, emite PlanArtifact
# ---------------------------------------------------------------------------
class RunPlannerTurnInput(BaseModel):
    # Reconciliação de contrato da integração da Fase 2: o worker do WS-B chama
    # esta Activity enviando `instructions` (lista, de clarification_notes) e
    # `base_branch`/`model_override` — não `instruction`/`branch`. Os fakes
    # lenientes dos testes de ambos os lados (aceitam dict) esconderam o
    # mismatch; o wire real quebrava com "missing instruction". Tornado
    # tolerante: `instruction` opcional derivada de `instructions`; aceita os
    # aliases do WS-B. Correção definitiva: promover este model + os de tester/
    # L2 a dse_contracts para uma única fonte da verdade (registrado no README).
    model_config = {"populate_by_name": True}

    work_item_id: str
    tenant_id: str
    instruction: str = ""
    instructions: list[str] = Field(default_factory=list)  # alias que o WS-B envia
    repo: str = "app"
    branch: str | None = None
    base_branch: str | None = None  # alias que o WS-B envia
    task_class: str = "default"
    data_class: str = "internal"
    diff_budget_lines: int = 400
    related_tickets: list[str] = Field(default_factory=list)
    model_override: str | None = None  # ignorado aqui; tolerado para não quebrar o decode

    @model_validator(mode="after")
    def _reconcile(self) -> "RunPlannerTurnInput":
        if not self.instruction and self.instructions:
            self.instruction = " ".join(s for s in self.instructions if s)
        if self.branch is None and self.base_branch is not None:
            self.branch = self.base_branch
        return self


def _default_plan_proposer(ctx: PlannerContext, inp: "RunPlannerTurnInput") -> dict[str, Any]:
    """Proposta MÍNIMA de plano quando nenhum substrato real está plugado —
    fixture claramente marcado (mesmo espírito do `FakeSubstrate` do Coder). Em
    produção, uma sessão OpenHands read-only (toolset planner) propõe
    steps/expected_files/test_plan a partir de `ctx.render()`; o override real
    é wireável por `_run_planner_turn_impl(..., proposer=...)`. Ver README."""
    return {
        "steps": [f"Analisar e implementar: {inp.instruction[:120]}"],
        "expected_files": [],
        "test_plan": "Adicionar/rodar testes cobrindo o comportamento novo (Tester turn).",
    }


@activity.defn(name=ACTIVITY_RUN_PLANNER_TURN)
async def run_planner_turn(inp: RunPlannerTurnInput) -> PlanArtifact:
    """Wrapper fino registrado como Activity Temporal (mesmo padrão de
    `run_coder_turn`). A lógica e os pontos de injeção para teste vivem em
    `_run_planner_turn_impl`."""
    return await _run_planner_turn_impl(inp)


async def _run_planner_turn_impl(
    inp: RunPlannerTurnInput,
    *,
    retrieval: RetrievalService | None = None,
    proposer=None,
    exploration_script: list[dict[str, Any]] | None = None,
    skills_conn=None,
) -> PlanArtifact:
    """Sessão Planner READ-ONLY (WSC-E3-T3).

    Toolset SÓ leitura: hidrata AGENTS.md + skill registry aprovado do tenant
    (E4) + CODEOWNERS + tickets relacionados + retrieval/index (E5), e emite um
    PlanArtifact estruturado. Qualquer tool de ESCRITA falha
    (`ToolPermissionError`) — a sessão usa `PlannerToolset`. P1: o `risk_class`
    é DERIVADO por `classify_risk_class` (determinístico), não pela palavra do
    LLM — é ele que dirige o gate do WS-B.
    """
    branch = inp.branch or _default_branch(inp.work_item_id)
    workspace_dir, _bare = _paths_for(inp.work_item_id)

    # Chamada de modelo (se houver) sai SÓ via gateway, stage=planner.
    headers = GatewayCallHeaders(
        tenant_id=inp.tenant_id,
        work_item_id=inp.work_item_id,
        stage=Stage.planner,
        task_class=inp.task_class,
        data_class=inp.data_class,
    )
    vk = mint_virtual_key(headers)

    retrieval = retrieval if retrieval is not None else RetrievalService()
    ctx = hydrate_planner_context(
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        workspace_dir=workspace_dir,
        repo=inp.repo,
        instruction=inp.instruction,
        task_class=inp.task_class,
        related_tickets=inp.related_tickets,
        retrieval=retrieval,
        skills_conn=skills_conn,
    )

    # Sessão read-only: qualquer step de escrita no exploration_script FALHA
    # aqui (toolset planner), o que é o teste de conformidade.
    session = ScriptedAgentSession(
        toolset=PlannerToolset(),
        workspace_dir=workspace_dir,
        retrieval=retrieval,
        tenant_id=inp.tenant_id,
        repo=inp.repo,
        context_reads={
            "read_agents_md": ctx.agents_md,
            "read_codeowners": ctx.codeowners,
            "list_skills": "\n".join(s.skill_key for s in ctx.skills),
        },
    )
    if exploration_script:
        session.run_script(exploration_script)

    proposal = (proposer or (lambda c: _default_plan_proposer(c, inp)))(ctx)
    expected_files = list(proposal.get("expected_files", []))
    forbidden = PlanArtifact.model_fields["forbidden_paths"].default_factory()
    risk_class = classify_risk_class(expected_files, inp.diff_budget_lines, forbidden)

    plan = PlanArtifact(
        work_item_id=inp.work_item_id,
        steps=list(proposal.get("steps", [])),
        expected_files=expected_files,
        diff_budget_lines=inp.diff_budget_lines,
        test_plan=proposal.get("test_plan", ""),
        risk_class=risk_class,
        forbidden_paths=forbidden,
    )

    audit_emit(
        actor="system:sandbox-runtime",
        action="planner_turn_completed",
        tenant_id=inp.tenant_id,
        work_item_id=inp.work_item_id,
        details={
            "stage": "planner",
            "steps": plan.steps,
            "expected_files": plan.expected_files,
            "risk_class": plan.risk_class,
            "diff_budget_lines": plan.diff_budget_lines,
            "skills_hydrated": [s.skill_key for s in ctx.skills],
            "retrieval_hits": [f"{h.repo}/{h.path}" for h in ctx.retrieval_hits],
            "virtual_key_fixture": vk.fixture,
        },
    )
    return plan


# ---------------------------------------------------------------------------
# run_tester_turn (WSC-E3-T4) — test runners + autoria de testes (só test paths)
# ---------------------------------------------------------------------------
class RunTesterTurnInput(BaseModel):
    # Reconciliação de contrato Fase 2 (ver nota em RunPlannerTurnInput): o
    # WS-B envia `plan`(dict)+`sandbox_id`+`model_override`+`runtime_override`,
    # não `instruction`. Tornado tolerante: instruction opcional derivada do
    # test_plan do plano; aliases do WS-B aceitos.
    model_config = {"populate_by_name": True}

    work_item_id: str
    tenant_id: str
    instruction: str = ""
    plan: dict | None = None  # alias que o WS-B envia (plan_json)
    sandbox_id: str | None = None
    repo: str = "app"
    branch: str | None = None
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
    """Retorno da Activity run_tester_turn. NÃO está em `packages/contracts`
    (WS-C não edita a fundação) — WS-B consome via dict/model_validate; ver
    README (proposta de promoção ao contrato na próxima janela do arquiteto)."""

    sandbox_id: str
    test_files: list[str]
    tests_ran: bool
    tests_passed: bool
    returncode: int
    cost_usd: float = 0.0
    # Reconciliação Fase 2: o WS-B decodifica o retorno desta Activity em
    # `CoderTurnResult` (fundação), que exige `diff_summary` + `files_changed`.
    # Expostos aqui como superset compatível (files_changed espelha test_files)
    # para o decode do WS-B não falhar — até a promoção ao contrato compartilhado.
    diff_summary: str = ""
    files_changed: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _mirror_test_files(self) -> "TesterTurnResult":
        if not self.files_changed:
            self.files_changed = list(self.test_files)
        if not self.diff_summary:
            self.diff_summary = f"tester: {len(self.test_files)} test file(s)"
        return self


@activity.defn(name=ACTIVITY_RUN_TESTER_TURN)
async def run_tester_turn(inp: RunTesterTurnInput) -> TesterTurnResult:
    return await _run_tester_turn_impl(inp)


async def _run_tester_turn_impl(
    inp: RunTesterTurnInput,
    *,
    retrieval: RetrievalService | None = None,
    authoring_script: list[dict[str, Any]] | None = None,
    push: bool = True,
) -> TesterTurnResult:
    """Sessão Tester (WSC-E3-T4): autoria de testes + runners. Edits permitidos
    SÓ em test paths (`TesterToolset` recusa write fora deles). Os testes
    escritos EXECUTAM de verdade (`run_tests` → pytest real no workspace), não
    são só gerados. O commit/push dos test files é determinístico
    (`ScopedGitSession`), nunca pelo LLM (P1)."""
    branch = inp.branch or _default_branch(inp.work_item_id)
    workspace_dir, _bare = _paths_for(inp.work_item_id)

    headers = GatewayCallHeaders(
        tenant_id=inp.tenant_id,
        work_item_id=inp.work_item_id,
        stage=Stage.tester,
        task_class=inp.task_class,
        data_class=inp.data_class,
    )
    vk = mint_virtual_key(headers)

    session = ScriptedAgentSession(
        toolset=TesterToolset(),
        workspace_dir=workspace_dir,
        retrieval=retrieval,
        tenant_id=inp.tenant_id,
        repo=inp.repo,
    )

    test_files: list[str] = []
    tests_ran = False
    tests_passed = False
    returncode = -1
    for step in authoring_script or []:
        res = session.invoke(step["tool"], **{k: v for k, v in step.items() if k != "tool"})
        if step["tool"] == "write_file":
            test_files.append(step["path"])
        if step["tool"] == "run_tests":
            tests_ran = True
            tests_passed = bool(res.detail.get("passed"))
            returncode = int(res.detail.get("returncode", -1))

    # Commit/push determinístico dos test files (só test paths foram escritos —
    # o toolset garantiu). Escapes de git ficam no código, nunca no LLM.
    git_session = ScopedGitSession(workspace_dir=workspace_dir, branch=branch)
    git_session.ensure_identity(name="dse-tester", email="tester@dse.local")
    if git_session.has_changes():
        git_session.commit(f"tester({inp.work_item_id}): {inp.instruction[:60]}")
        if push:
            try:
                git_session.push()
            except GitScopeViolation:
                audit_emit(
                    actor="system:sandbox-runtime",
                    action="tester_push_rejected",
                    tenant_id=inp.tenant_id,
                    work_item_id=inp.work_item_id,
                    details={"branch": branch},
                )
                raise

    audit_emit(
        actor="system:sandbox-runtime",
        action="tester_turn_completed",
        tenant_id=inp.tenant_id,
        work_item_id=inp.work_item_id,
        details={
            "stage": "tester",
            "test_files": test_files,
            "tests_ran": tests_ran,
            "tests_passed": tests_passed,
            "returncode": returncode,
            "virtual_key_fixture": vk.fixture,
        },
    )
    return TesterTurnResult(
        sandbox_id=inp.work_item_id,
        test_files=test_files,
        tests_ran=tests_ran,
        tests_passed=tests_passed,
        returncode=returncode,
        cost_usd=0.0,
    )


# ---------------------------------------------------------------------------
# run_l2_review (WSC-E3-T5) — sessão Reviewer fresh-context, retorna L2Verdict
# ---------------------------------------------------------------------------
class RunL2ReviewInput(BaseModel):
    """Entrada da Activity L2. Carrega SÓ plan + diff — deliberadamente NÃO
    existe campo para histórico/transcrição do Coder (P3 por construção; o WS-B
    não tem como injetar contexto do produtor nesta Activity nem se quisesse)."""

    # P3 (NÃO-NEGOCIÁVEL, joia da coroa): os campos são EXATAMENTE
    # {work_item_id, tenant_id, plan, diff, task_class, data_class} — nenhum
    # canal para histórico/instrução do Coder. Este model é deliberadamente
    # ESTRITO e NÃO foi alargado na reconciliação da Fase 2 (ao contrário de
    # planner/tester): quem se adapta é o CHAMADOR (WS-B envia `diff`, não
    # `diff_summary` — corrigido no call site do workflow, não aqui).
    work_item_id: str
    tenant_id: str
    plan: PlanArtifact
    diff: str
    task_class: str = "default"
    data_class: str = "internal"


def _changed_files_from_diff(diff: str) -> list[str]:
    files: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            files.append(line[6:].strip())
        elif line.startswith("diff --git a/"):
            # "diff --git a/x b/x"
            parts = line.split(" b/", 1)
            if len(parts) == 2:
                files.append(parts[1].strip())
    # dedup preservando ordem
    seen: set[str] = set()
    out = []
    for f in files:
        if f and f not in seen:
            seen.add(f)
            out.append(f)
    return out


def _default_reviewer_verdict(ctx: ReviewerContext):
    """Reviewer determinístico de STAND-IN (fixture claramente marcado, mesmo
    espírito do FakeSubstrate). Julga aderência do diff ao plano por regras
    objetivas: (a) nenhum arquivo alterado fora do blast radius declarado
    (`expected_files`, se não vazio); (b) nenhum arquivo em `forbidden_paths`.
    Em produção, uma sessão OpenHands FRESCA (só plan+diff) substitui isto e
    devolve objeções de convenção/lógica com arquivo/linha — override via
    `_run_l2_review_impl(..., verdict_fn=...)`. Ver README."""
    changed = _changed_files_from_diff(ctx.diff)
    objections: list[str] = []
    expected = set(ctx.plan.expected_files)
    for f in changed:
        if expected and f not in expected:
            objections.append(f"{f}: alterado fora do blast radius declarado no plano (expected_files)")
        for fb in ctx.plan.forbidden_paths:
            if f.startswith(fb.rstrip("*")):
                objections.append(f"{f}: toca forbidden_path '{fb}' — requer caminho humano")
    return (len(objections) == 0, objections, 0.0)


@activity.defn(name=ACTIVITY_RUN_L2_REVIEW)
async def run_l2_review(inp: RunL2ReviewInput) -> L2Verdict:
    return await _run_l2_review_impl(inp)


async def _run_l2_review_impl(inp: RunL2ReviewInput, *, verdict_fn=None) -> L2Verdict:
    """Constrói a sessão Reviewer de contexto FRESCO (WSC-E3-T5) e devolve o
    `L2Verdict`. A sessão recebe SÓ `ReviewerContext(plan, diff)` — nunca o
    histórico do Coder (P3). O veredito é RECOMENDAÇÃO (gateia progressão); o
    merge continua humano (P1)."""
    context = ReviewerContext(work_item_id=inp.work_item_id, plan=inp.plan, diff=inp.diff)
    session = FreshReviewerSession(context)
    verdict = session.review(verdict_fn or _default_reviewer_verdict)

    audit_emit(
        actor="system:sandbox-runtime",
        action="l2_review_completed",
        tenant_id=inp.tenant_id,
        work_item_id=inp.work_item_id,
        details={
            "stage": "reviewer",
            "passed": verdict.passed,
            "objections": verdict.objections,
            "fresh_context": True,
            "context_fields": sorted(type(context).__dataclass_fields__.keys()),
        },
    )
    return verdict


# Consumido pelo loader defensivo do worker unico (services/orchestrator/
# src/dse_orchestrator/worker.py:_load_cross_workstream_activities) — nome
# `ACTIVITIES` e o contrato que o integrador espera (ver docstring de lá).
ACTIVITIES = [
    provision_sandbox,
    checkpoint_sandbox,
    rebuild_sandbox,
    teardown_sandbox,
    run_coder_turn,
    run_planner_turn,
    run_tester_turn,
    run_l2_review,
]
