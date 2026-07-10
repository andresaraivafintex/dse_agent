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

from pydantic import BaseModel, Field
from temporalio import activity

from dse_audit import emit as audit_emit
from dse_contracts import (
    ACTIVITY_CHECKPOINT_SANDBOX,
    ACTIVITY_PROVISION_SANDBOX,
    ACTIVITY_REBUILD_SANDBOX,
    ACTIVITY_RUN_CODER_TURN,
    ACTIVITY_TEARDOWN_SANDBOX,
    CheckpointRef,
    CoderTurnResult,
    GatewayCallHeaders,
    SandboxHandle,
    Stage,
)

from . import docker_driver, git_checkpoint, leases_store, metrics
from .model_gateway_client import mint_virtual_key
from .scoped_git import GitScopeViolation, ScopedGitSession
from .substrate import AgentSubstrate, FakeSubstrate

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


# Consumido pelo loader defensivo do worker unico (services/orchestrator/
# src/dse_orchestrator/worker.py:_load_cross_workstream_activities) — nome
# `ACTIVITIES` e o contrato que o integrador espera (ver docstring de lá).
ACTIVITIES = [
    provision_sandbox,
    checkpoint_sandbox,
    rebuild_sandbox,
    teardown_sandbox,
    run_coder_turn,
]
