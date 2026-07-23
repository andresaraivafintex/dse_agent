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

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("sandbox_runtime.activities")

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
from .activity_heartbeat import run_sync_with_heartbeat
from .driver import DEFAULT_SANDBOX_DRIVER
from .model_gateway_client import mint_virtual_key
from .retrieval import RetrievalService
from .runtime_profile import (
    RuntimeProfile,
    reject_local_agent_execution,
    validate_runtime_profile,
    validate_runtime_startup,
)
from .scoped_git import GitScopeViolation, ScopedGitSession
from .skill_files import materialize_skills, workspace_skills_note
from .sessions import (
    FreshReviewerSession,
    PlannerContext,
    ReviewerContext,
    ScriptedAgentSession,
    classify_risk_class,
    hydrate_planner_context,
)
from .substrate import SUBSTRATE_ENV_VAR, AgentSubstrate, FakeSubstrate, substrate_from_env
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
    repo: str | None = None  # S4: repo alvo (ex. "andre2654/fintex-wallet") a clonar
    budget: dict[str, Any] = Field(default_factory=dict)
    image: str | None = None


@activity.defn(name=ACTIVITY_PROVISION_SANDBOX)
async def provision_sandbox(inp: ProvisionSandboxInput) -> SandboxHandle:
    profile = validate_runtime_startup()
    branch = inp.branch or _default_branch(inp.work_item_id)
    workspace_dir, bare_repo_path = _paths_for(inp.work_item_id)

    is_new_checkpoint_repo = not Path(bare_repo_path).exists()
    if is_new_checkpoint_repo:
        git_checkpoint.provision_checkpoint_repo(bare_repo_path, branch)
    if not Path(workspace_dir).exists():
        # S4 (Fase 5): se a tarefa tem um repo alvo (ex.: github.com/andre2654/
        # fintex-wallet), CLONA o código real (com token minto no control plane
        # e scrubbado do config) — o Coder trabalha no repo de verdade. Sem
        # repo/token (testes), cai para o workspace vazio da mecânica original.
        cloned = False
        if inp.repo:
            from . import repo_clone
            token = repo_clone.mint_installation_token()
            cloned = repo_clone.clone_repo_into(
                workspace_dir=workspace_dir, repo=inp.repo,
                base_branch=inp.base_branch, task_branch=branch,
                bare_repo_path=bare_repo_path, token=token,
            )
            if cloned and not repo_clone.token_absent_from_config(workspace_dir):
                raise RuntimeError("SEGURANCA: token vazou no git config do workspace")
            if not cloned and profile is RuntimeProfile.production:
                validate_runtime_profile(
                    local_fallback=(
                        f"clone de {inp.repo!r} falhou/sem credencial e cairia para workspace vazio"
                    )
                )
        if not cloned:
            git_checkpoint.init_task_workspace(workspace_dir, bare_repo_path, branch, inp.base_branch)

    # Skills tickadas para o repo (console → skill_registry.repo_scope, 0029)
    # materializadas AQUI — depois do clone, workspace garantidamente git.
    # Guidance é best-effort no provision (o Planner continua falhando limpo
    # se o registry cair — a leitura mandatória é a dele); qualquer skip fica
    # auditado (P8).
    try:
        from .skill_files import materialize_skills as _materialize
        from .skill_registry import read_approved_skills as _read_skills
        _mat = _materialize(workspace_dir, _read_skills(inp.tenant_id, repo=inp.repo))
        if _mat:
            audit_emit(
                actor="system:sandbox-runtime",
                action="skills_materialized",
                tenant_id=inp.tenant_id,
                work_item_id=inp.work_item_id,
                details={"skills": _mat, "repo": inp.repo},
            )
    except Exception as exc:  # noqa: BLE001 — guidance não derruba o provision
        audit_emit(
            actor="system:sandbox-runtime",
            action="skills_materialization_skipped",
            tenant_id=inp.tenant_id,
            work_item_id=inp.work_item_id,
            details={"reason": f"{type(exc).__name__}: {str(exc)[:200]}"},
        )

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
    validate_runtime_startup()
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
# Contrato CANÔNICO (anti-shadow — mesmo achado do L1: o model local
# descartava silenciosamente campos que o workflow envia, ex.: model_override
# e o novo expected_files). Nunca redefina modelos de contrato localmente.
from dse_contracts.activities import RunCoderTurnInput  # noqa: E402


def _build_substrate(script: list[dict[str, Any]] | None) -> AgentSubstrate:
    """Fábrica de substrato. Fase 3 (WSC-E3-T6): a escolha é CONFIG POR
    DEPLOYMENT — `DSE_CODER_SUBSTRATE` em {fake|openhands|claude-agent},
    default `fake` (nenhuma dependência de gateway/SDK precisa estar de pé
    para os testes). Trocar de substrato nunca muda código de workflow: o
    WS-B continua chamando `run_coder_turn` por nome, e esta factory resolve
    o adapter atrás da mesma interface `AgentSubstrate`."""
    return substrate_from_env(script=script)


def _prune_disposable_artifacts(
    workspace_dir: str, expected_files: list[str], work_item_id: str
) -> tuple[list[str], list[str]]:
    """Camada 2 (determinística, P1) do anti-relatório-espontâneo: apaga
    arquivos NOVOS (untracked) que são LIXO óbvio do CLI (log/scratch/backup e
    relatórios como BUG_FIX_REPORT.md), ANTES do commit.

    Reconciliado com a política nova (2026-07-22): como `expected_files` virou
    advisory no L1 (ver a memória l1-expected-files-advisory), NÃO apagamos mais
    "tudo que está fora do plano" — só o descartável (`is_disposable_artifact`).
    Um arquivo-fonte NOVO e legítimo que o fix precisou criar SOBREVIVE, mesmo
    fora de `expected_files`. Nunca toca: o que o plano pediu, testes, ou o demo
    do work item; e só olha untracked (`??`) — um arquivo EXISTENTE modificado
    fora do plano fica e é o L1/orçamento que julga.

    Best-effort (o L1 é o gate duro): falha de git → não apaga nada. Retorna
    `(pruned, kept_out_of_plan)`; `kept_out_of_plan` é o que a política antiga
    teria apagado e agora preserva — emitido no audit para o operador ver a
    reconciliação em ação.
    """
    from dse_contracts.paths import is_disposable_artifact, is_test_path

    import subprocess as _sp

    try:
        # `-uall`: lista CADA arquivo untracked individualmente. Sem ele, o git
        # colapsa um diretório inteiramente novo num único `?? src/` — e um
        # arquivo-fonte dentro de um diretório NOVO nunca seria visto no nível
        # de arquivo (o prune inline antigo silenciava esse caso no OSError de
        # remover um diretório). .gitignore continua respeitado (não lista
        # node_modules etc.).
        porcelain = _sp.run(
            ["git", "status", "--porcelain", "-uall"], cwd=workspace_dir,
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:  # noqa: BLE001 — prune é best-effort; o L1 é o gate duro
        logger.warning("prune pós-coder falhou (git status); o L1 segue como gate")
        return [], []

    expected = set(expected_files)
    pruned: list[str] = []
    kept_out_of_plan: list[str] = []
    for line in porcelain.splitlines():
        if not line.startswith("??"):
            continue
        rel = line[3:].strip().strip('"')
        # Nunca poda o que o plano pediu, testes, ou o demo do work item.
        if rel in expected or is_test_path(rel) or rel.startswith(f"demos/{work_item_id}"):
            continue
        if not is_disposable_artifact(rel):
            kept_out_of_plan.append(rel)  # fonte nova legítima fora do plano — FICA
            continue
        try:
            os.remove(os.path.join(workspace_dir, rel))
            pruned.append(rel)
        except OSError:
            pass
    return pruned, kept_out_of_plan


def _revert_coder_test_edits(workspace_dir: str, turn_start_sha: str) -> list[str]:
    """O Coder NÃO é dono dos testes — o Tester os autora em arquivos ISOLADOS
    (achado do disparo real na issue #1: o Coder editou o seed compartilhado do
    `before()` em test/api.test.js, movendo uma transação de julho→junho, e
    quebrou um teste IRMÃO pré-existente que fixava a ordenação por data. O fix
    de código estava certo; o loop era 100% esse conflito teste-vs-teste, que
    consertar summary.js não resolve).

    Reverte QUALQUER mudança do Coder em test paths ao estado do INÍCIO do turno
    (`turn_start_sha`): edição/remoção de teste existente → `git checkout <sha>`;
    teste NOVO (untracked) → remove. Aplicado todo turno, nenhum commit do Coder
    carrega mudança de teste — o Tester (etapa seguinte) autora os testes limpo.
    Best-effort: falha de git → não reverte (o L1/Tester ainda são os gates).
    Retorna os paths revertidos."""
    from dse_contracts.paths import is_test_path

    import subprocess as _sp

    try:
        porcelain = _sp.run(
            ["git", "status", "--porcelain", "-uall"], cwd=workspace_dir,
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:  # noqa: BLE001 — best-effort
        logger.warning("revert de testes do coder falhou (git status)")
        return []

    reverted: list[str] = []
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        status, rel = line[:2], line[3:].strip().strip('"')
        if "->" in rel:  # rename: "old -> new" — pega o destino
            rel = rel.split("->")[-1].strip()
        if not is_test_path(rel):
            continue
        try:
            if status.strip() == "??":
                os.remove(os.path.join(workspace_dir, rel))
                reverted.append(rel)
            else:
                proc = _sp.run(
                    ["git", "checkout", turn_start_sha, "--", rel], cwd=workspace_dir,
                    capture_output=True, text=True, timeout=30,
                )
                if proc.returncode == 0:
                    reverted.append(rel)
        except OSError:
            continue
    return reverted


@activity.defn(name=ACTIVITY_RUN_CODER_TURN)
async def run_coder_turn(inp: RunCoderTurnInput) -> CoderTurnResult:
    """Wrapper fino registrado como Activity de verdade — Temporal não aceita
    argumentos extras (nem keyword-only, nem posicionais opcionais) em
    funções decoradas com `@activity.defn`. A lógica real e os pontos de
    injeção de dependência para teste (`substrate`/`script`) vivem em
    `_run_coder_turn_impl`, chamada tanto por aqui (produção, sem overrides)
    quanto diretamente pelos testes (com `FakeSubstrate` roteirizado)."""
    reject_local_agent_execution("coder")
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

    # Âncora do plano na instrução (achado do disparo real: o CLI cria
    # relatórios espontâneos — BUG_FIX_REPORT.md). Camada 1: instrução
    # explícita; camada 2 (determinística): prune pós-turn SÓ de artefatos
    # descartáveis (relatório/log/scratch), nunca de fonte nova legítima —
    # ver _prune_disposable_artifacts.
    if inp.expected_files:
        inp.instruction += (
            "\n\n## Plan constraints (mandatory)\n"
            f"- Modify ONLY production code in these files: {', '.join(inp.expected_files)}.\n"
            "- Do NOT create or edit TEST files (tests/, *.test.js, test_*.py…). "
            "Writing tests is a SEPARATE stage (the Tester) — any test change you "
            "make is reverted before the commit.\n"
            "- Do NOT create documentation/report files (README, *_REPORT.md, "
            "CHANGELOG…) — the change and the tests speak for themselves."
        )

    # Skills do repo (ticks do console materializados no turno do Planner +
    # skills commitadas no repo alvo): o ClaudeAgentSubstrate as carrega via
    # setting_sources=["project"]; a nota cobre os demais substratos.
    inp.instruction += workspace_skills_note(workspace_dir)

    base_sha_session = ScopedGitSession(workspace_dir=workspace_dir, branch=branch)
    base_sha = base_sha_session.current_sha()

    done = False
    max_turns = 8
    turns = 0
    while not done and turns < max_turns:
        try:
            log = await run_sync_with_heartbeat(
                agent.run_turn,
                inp.instruction,
                stage=inp.stage,
                work_item_id=inp.work_item_id,
                operation=f"substrate_turn_{turns + 1}",
            )
        except Exception as exc:  # noqa: BLE001 — classificação, não engolimento
            _raise_if_permanent_provider_error(exc)
            raise
        done = log.done
        turns += 1

    artifacts = agent.collect_artifacts()

    # Camada 2 (determinística, P1): apaga arquivos NOVOS (untracked) que são
    # LIXO óbvio do CLI (relatório espontâneo/log/scratch) antes do commit — NÃO
    # mais "tudo fora do plano" (expected_files virou advisory no L1; um arquivo-
    # fonte novo legítimo fora do plano SOBREVIVE). Arquivos EXISTENTES
    # modificados fora do plano ficam — é o L1/orçamento que os julga.
    if inp.expected_files:
        pruned, kept_out_of_plan = _prune_disposable_artifacts(
            workspace_dir, inp.expected_files, inp.work_item_id
        )
        if pruned:
            audit_emit(
                actor="system:sandbox-runtime",
                action="coder_out_of_plan_files_pruned",
                tenant_id=inp.tenant_id,
                work_item_id=inp.work_item_id,
                details={"pruned": pruned[:20]},
            )
        if kept_out_of_plan:
            # Observabilidade da reconciliação (2026-07-22): sob a política antiga
            # estes NOVOS fora do plano seriam apagados; agora ficam (expected_files
            # é advisory) e quem julga é o L1 (orçamento de linhas + forbidden_paths).
            audit_emit(
                actor="system:sandbox-runtime",
                action="coder_out_of_plan_files_kept",
                tenant_id=inp.tenant_id,
                work_item_id=inp.work_item_id,
                details={"kept_out_of_plan": kept_out_of_plan[:20]},
            )

    _restore_lockfile_churn_audited(workspace_dir, inp.tenant_id, inp.work_item_id, stage="coder")

    # O Coder NÃO é dono dos testes — o Tester os autora em arquivos ISOLADOS
    # (achado do disparo real na issue #1: o Coder editou o seed compartilhado
    # de test/api.test.js e quebrou um teste IRMÃO pré-existente → o fix cycle
    # nunca converge, porque consertar summary.js não conserta o teste). Reverte
    # QUALQUER mudança do Coder em test paths ao estado do início do turno.
    reverted_tests = _revert_coder_test_edits(workspace_dir, base_sha)
    if reverted_tests:
        audit_emit(
            actor="system:sandbox-runtime",
            action="coder_test_edits_reverted",
            tenant_id=inp.tenant_id,
            work_item_id=inp.work_item_id,
            details={"reverted": reverted_tests[:20], "reason": "testes são da etapa Tester"},
        )

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
# PROMOVIDO ao contrato (adendo 02 §2.3, gate de entrada da Fase 3): a
# definição canônica vive em `dse_contracts.activities` com testes de
# regressão de boundary (packages/contracts/tests/test_activity_boundaries.py)
# validando os payloads exatos do WS-B. Re-import para compatibilidade — todo
# consumidor local (testes, sessions) continua funcionando sem mudança.
from dse_contracts import RunPlannerTurnInput  # noqa: E402


def _default_plan_proposer(ctx: PlannerContext, inp: "RunPlannerTurnInput") -> dict[str, Any]:
    """Proposta MÍNIMA de plano quando nenhum substrato real está plugado —
    fixture claramente marcado (mesmo espírito do `FakeSubstrate` do Coder).
    Com a guarda anti-PR-oco do WS-B (`planner_expected_files_empty_...`), um
    plano deste fixture ESCALA no gate — comportamento deliberado: sem modelo
    real, o DSE não finge planejar."""
    return {
        "steps": [f"Analyze and implement: {inp.instruction[:120]}"],
        "expected_files": [],
        "test_plan": "Add/run tests covering the new behavior (Tester turn).",
    }


_PLAN_PROMPT = """You are the Planner of Fintex DSE (an autonomous software engineer).
Based on the task below, produce a MINIMAL, verifiable implementation plan.

Respond ONLY with a valid JSON object (no markdown, no comments), in the format:
{{"steps": ["step 1", "step 2", ...],
  "expected_files": ["relative/path/1", "path/2", ...],
  "test_plan": "how to verify the change"}}

Rules:
- "expected_files": the files that will be CREATED/EDITED (relative to the root).
  {files_rule}
  The implementation diff will be validated AGAINST this list (test files
  are exempt) — include ALL production files that may change. NEVER
  empty.
- 2 to 6 steps, specific and executable.
- The plan must solve EXACTLY the task in the "Task" section — nothing beyond it
  (no extra feature/refactor, however useful it may seem).
- Do not include anything besides the JSON.

## Task
{instruction}

## Additional context (skills/AGENTS.md/retrieval — may be empty)
{context}
{tree_section}"""


def _repo_tree_for_planner(repo: str, base_branch: str) -> list[str]:
    """Árvore REAL do repo no branch base (best-effort, via GitHub API do
    control plane) — sem ela o Planner adivinha caminhos e o plan_compliance
    reprova o diff real (achado do disparo real). Falha → lista vazia (o
    prompt degrada para 'caminhos prováveis')."""
    try:
        from dse_validation.config import GitHubConfig
        from dse_validation.github.client import build_github_client

        client = build_github_client(GitHubConfig())
        return client.get_tree_paths(repo, base_branch or "main")
    except Exception as exc:  # noqa: BLE001 — árvore é contexto, não requisito
        logger.warning("árvore do repo indisponível p/ o planner (%s: %s)",
                       type(exc).__name__, str(exc)[:120])
        return []


def _model_plan_proposer(
    ctx: PlannerContext, inp: "RunPlannerTurnInput", headers: Any, virtual_key: str
) -> dict[str, Any] | None:
    """Plano proposto pelo MODELO REAL via gateway (stage=planner, virtual key,
    enforcement + ledger de custo no caminho — WSD). Retorna None em qualquer
    falha (import ausente, chamada recusada, JSON inválido) — o caller cai no
    fixture e a guarda do WS-B escala LIMPO (P6), nunca um plano inventado.

    P1 preservado: o modelo só PROPÕE steps/expected_files/test_plan; risco e
    gates continuam derivados deterministicamente (classify_risk_class)."""
    try:
        from model_gateway_client.gateway_call import chat_completion
    except ImportError:
        logger.warning("model_gateway_client indisponível — planner segue no fixture")
        return None

    model = os.environ.get("DSE_PLANNER_MODEL") or os.environ.get("DSE_CODER_MODEL", "anthropic/claude")
    tree = _repo_tree_for_planner(inp.repo, inp.base_branch or "main")
    if tree:
        files_rule = "Use ONLY paths from the tree below (or new ones consistent with it)."
        tree_section = "\n## Repo tree (base branch)\n" + "\n".join(tree[:250])
    else:
        files_rule = "Propose the most likely paths per the ecosystem's conventions."
        tree_section = ""
    # A INSTRUÇÃO entra direto no prompt (3º disparo real: PlannerContext não
    # carrega a instrução — render() só tem AGENTS.md/skills/repo map, todos
    # vazios neste tenant — e o modelo, sem nunca VER a issue, planejou uma
    # feature genérica de wallet em vez do bug de DELETE).
    prompt = _PLAN_PROMPT.format(
        instruction=(inp.instruction or "").strip()[:6000] or "(instruction missing)",
        context=ctx.render()[:8000],
        files_rule=files_rule,
        tree_section=tree_section[:8000],
    )
    try:
        result = chat_completion(
            headers=headers,
            virtual_key=virtual_key,
            model=model,
            messages=[{"role": "user", "content": prompt}],
            timeout=120.0,
            max_tokens=1500,
            temperature=0,
        )
    except Exception as exc:  # noqa: BLE001 — recusa/erro => fixture (escala limpa)
        _raise_if_permanent_provider_error(exc)  # billing/auth: mensagem certa na issue
        logger.warning("planner via modelo falhou (%s: %s) — fixture", type(exc).__name__, str(exc)[:200])
        return None

    text = (result.content or "").strip()
    if text.startswith("```"):
        text = text.strip("`\n")
        text = text[4:] if text.startswith("json") else text
    try:
        proposal, _ = json.JSONDecoder().raw_decode(text.strip())
        steps = [str(s) for s in proposal.get("steps", []) if str(s).strip()]
        files = [str(f) for f in proposal.get("expected_files", []) if str(f).strip()]
        if not steps or not files:
            raise ValueError("steps/expected_files vazios")
        return {
            "steps": steps[:10],
            "expected_files": files[:30],
            "test_plan": str(proposal.get("test_plan") or "Cover the change with tests (Tester turn)."),
        }
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("plano do modelo não parseou (%s) — fixture; resposta: %.200s", exc, text)
        return None


@activity.defn(name=ACTIVITY_RUN_PLANNER_TURN)
async def run_planner_turn(inp: RunPlannerTurnInput) -> PlanArtifact:
    """Wrapper fino registrado como Activity Temporal (mesmo padrão de
    `run_coder_turn`). A lógica e os pontos de injeção para teste vivem em
    `_run_planner_turn_impl`."""
    reject_local_agent_execution("planner")
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

    # Skills em arquivo (`.claude/skills/`) são materializadas pelo
    # provision_sandbox (após o clone — o Planner pode rodar ANTES do
    # provision e o workspace ainda não existir aqui). Este re-materialize é
    # no-op nesse caso (guard de `.git` no skill_files) e atualiza o workspace
    # quando o registry mudou entre retries.
    skills_materialized = materialize_skills(workspace_dir, ctx.skills)

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
        await run_sync_with_heartbeat(
            session.run_script,
            exploration_script,
            stage=Stage.planner.value,
            work_item_id=inp.work_item_id,
            operation="planner_exploration",
        )

    # Seleção do proposer (P1 — por CONFIG, nunca por modelo):
    #   proposer explícito (testes) > modelo real (substrato != fake) com
    #   fallback ao fixture > fixture. O fixture tem expected_files vazio e a
    #   guarda do WS-B escala — deliberado quando não há modelo.
    if proposer is not None:
        proposal_fn = proposer
    elif os.environ.get(SUBSTRATE_ENV_VAR, "fake").strip().lower() != "fake":
        def proposal_fn(c):  # noqa: ANN001 — assinatura do run_sync_with_heartbeat
            return (
                _model_plan_proposer(c, inp, headers, vk.virtual_key)
                or _default_plan_proposer(c, inp)
            )
    else:
        proposal_fn = lambda c: _default_plan_proposer(c, inp)  # noqa: E731
    proposal = await run_sync_with_heartbeat(
        proposal_fn,
        ctx,
        stage=Stage.planner.value,
        work_item_id=inp.work_item_id,
        operation="planner_proposal",
    )
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
            "skills_materialized": skills_materialized,
            "retrieval_hits": [f"{h.repo}/{h.path}" for h in ctx.retrieval_hits],
            "virtual_key_fixture": vk.fixture,
        },
    )
    return plan


# ---------------------------------------------------------------------------
# run_tester_turn (WSC-E3-T4) — test runners + autoria de testes (só test paths)
# ---------------------------------------------------------------------------
# PROMOVIDOS ao contrato (adendo 02 §2.3) — definição canônica em
# `dse_contracts.activities`, testes de boundary na fundação. Re-import para
# compatibilidade dos consumidores locais.
from dse_contracts import RunTesterTurnInput, TesterTurnResult  # noqa: E402


# Padrões de erro PERMANENTE do provider (achado do disparo real 2026-07-22):
# créditos esgotados/key inválida não são transitórios — retentar é loop
# infinito de attempts. Lançados non_retryable; o workflow os converte em
# _FailClosed → falha limpa comentada na issue (P6).
_PERMANENT_PROVIDER_MARKERS = (
    "credit balance is too low",
    "plans & billing",
    "insufficient credits",
    "authentication_error",
    "invalid x-api-key",
)


def _raise_if_permanent_provider_error(exc: Exception) -> None:
    blob = f"{type(exc).__name__}:{exc}".lower()
    if any(m in blob for m in _PERMANENT_PROVIDER_MARKERS):
        from temporalio.exceptions import ApplicationError

        raise ApplicationError(
            f"provider_billing_or_auth: {str(exc)[:200]}",
            type="ProviderBillingError",
            non_retryable=True,
        ) from exc


_TEST_AUTHOR_PROMPT = """You are the Tester of Fintex DSE. Write AUTOMATED test(s) that
verify the described change — ideally reproducing the bug (they fail without the fix,
pass with it).

Respond ONLY with valid JSON (no markdown):
{{"files": [{{"path": "relative/path/of/the/test", "content": "full file content"}}]}}

CRITICAL RULES:
- Use EXACTLY the runner and style of the EXISTING TEST shown below (same
  imports, same structure). Do NOT use jest/mocha/vitest/supertest or ANY
  package that is not in the dependencies of the package.json shown — the repo
  may have no dependencies at all (native runner).
- Create ONLY NEW file(s) — NEVER rewrite an existing test.
  FORBIDDEN PATHS (already exist): {existing_tests}
  Use a new name, e.g.: test/<subject>-dse.test.js
- Paths MUST be test paths (tests/, __tests__/, *.test.js|ts, test_*.py…).
- 1 file (2 at most); CONCISE (~40-80 lines). Truncated JSON = failure.
- Do not modify production code — tests only.
{error_feedback}
## Task
{instruction}

## Plan
{plan}

## Repo package.json (REAL runner/dependencies)
{package_json}

## EXISTING test from the repo (IMITATE this style/runner)
{example_test}

## Coder's change (diff)
{diff}
"""


def _tester_repo_context(workspace_dir: str) -> tuple[str, str, set[str]]:
    """Contexto determinístico para a autoria imitar o repo REAL (achado do
    disparo real: o modelo escreveu Jest num repo node:test sem deps e ainda
    sobrescreveu o teste original — nada rodava nunca). Retorna
    (package_json, exemplo_de_teste, paths_de_teste_existentes)."""
    from dse_contracts.paths import is_test_path

    pkg = ""
    try:
        pkg = open(os.path.join(workspace_dir, "package.json")).read()[:1500]
    except OSError:
        pkg = "(no package.json — likely Python/pytest)"
    existing: set[str] = set()
    example = ""
    for root, _dirs, files in os.walk(workspace_dir):
        if "/.git" in root or "/node_modules" in root:
            continue
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), workspace_dir)
            if is_test_path(rel):
                existing.add(rel)
                if not example:
                    try:
                        example = f"# {rel}\n" + open(os.path.join(root, f)).read()[:3000]
                    except OSError:
                        pass
    return pkg, example or "(no existing tests — use the ecosystem's default runner)", existing


def _model_authored_test_script(
    inp: "RunTesterTurnInput", workspace_dir: str, headers: Any, virtual_key: str,
    *, error_feedback: str = "",
) -> list[dict[str, Any]] | None:
    """Autoria de testes pelo MODELO REAL (mesmo padrão do planner): 1 chamada
    stage=tester via gateway → arquivos de teste → script determinístico
    [write_file..., run_tests]. Guard-rails determinísticos (P1):
      - contexto de IMITAÇÃO: package.json + um teste existente do repo (o
        modelo copia o runner real, nunca inventa jest/supertest);
      - paths fora de test paths OU de testes JÁ EXISTENTES são recusados
        (sobrescrever teste do repo destruiria a suíte);
      - `error_feedback` re-injeta o erro de infra da 1ª tentativa (1 retry).
    Qualquer falha → None → tests_ran=False → o gate do WS-B para limpo."""
    try:
        from model_gateway_client.gateway_call import chat_completion
    except ImportError:
        logger.warning("model_gateway_client indisponível — tester sem autoria real")
        return None
    from dse_contracts.paths import is_test_path

    diff = ""
    try:
        import subprocess as _sp
        proc = _sp.run(["git", "show", "--stat", "-p", "HEAD"],
                       cwd=workspace_dir, capture_output=True, text=True, timeout=30)
        diff = proc.stdout[-8000:]
    except Exception:  # noqa: BLE001 — diff é contexto, não requisito
        pass

    package_json, example_test, existing_tests = _tester_repo_context(workspace_dir)
    model = os.environ.get("DSE_TESTER_MODEL") or os.environ.get("DSE_CODER_MODEL", "anthropic/claude")
    prompt = _TEST_AUTHOR_PROMPT.format(
        instruction=(inp.instruction or "")[:3000],
        plan=json.dumps(inp.plan or {}, ensure_ascii=False)[:1500],
        package_json=package_json,
        example_test=example_test,
        existing_tests=", ".join(sorted(existing_tests)) or "(none)",
        diff=diff or "(diff unavailable)",
        error_feedback=(
            f"\n## ERROR FROM THE PREVIOUS ATTEMPT (fix it!)\n{error_feedback}\n" if error_feedback else ""
        ),
    )
    # Skills do repo (materializadas pelo Planner + commitadas no repo alvo):
    # o Tester também deve seguir a guidance (estilo de teste, padrões do tenant).
    prompt += workspace_skills_note(workspace_dir)[:2000]
    try:
        result = chat_completion(
            headers=headers, virtual_key=virtual_key, model=model,
            messages=[{"role": "user", "content": prompt}],
            # 8000: achado do disparo real com Haiku — 4000 truncava o JSON no
            # meio do content ("Unterminated string") e a autoria inteira caía.
            timeout=180.0, max_tokens=8000, temperature=0,
        )
    except Exception as exc:  # noqa: BLE001
        _raise_if_permanent_provider_error(exc)  # billing/auth: mensagem certa na issue
        logger.warning("tester via modelo falhou (%s: %s)", type(exc).__name__, str(exc)[:200])
        return None

    text = (result.content or "").strip()
    if text.startswith("```"):
        text = text.strip("`\n")
        text = text[4:] if text.startswith("json") else text
    try:
        parsed, _ = json.JSONDecoder().raw_decode(text.strip())
        files = parsed.get("files") or []
    except json.JSONDecodeError as exc:
        logger.warning("autoria de teste não parseou (%s); resposta: %.200s", exc, text)
        return None

    script: list[dict[str, Any]] = []
    for f in files[:3]:
        path, content = str(f.get("path") or ""), str(f.get("content") or "")
        if not (path and content and is_test_path(path)):
            logger.warning("path de teste recusado (fora de test paths): %r", path)
            continue
        if path in existing_tests:
            # Em vez de descartar (deixava o script vazio quando o modelo
            # insistia no teste existente), RENOMEIA deterministicamente para
            # um arquivo novo no MESMO diretório — imports relativos intactos.
            renamed = _dedupe_test_path(path, existing_tests, workspace_dir)
            logger.warning("path de teste JÁ EXISTE — renomeado %r → %r", path, renamed)
            path = renamed
        script.append({"tool": "write_file", "path": path, "content": content})
    if not script:
        return None
    script.append({"tool": "run_tests"})
    return script


def _dedupe_test_path(path: str, existing: set[str], workspace_dir: str) -> str:
    """Nome novo no mesmo diretório, ainda casando is_test_path:
    test/api.test.js → test/api-dse.test.js; tests/test_x.py → tests/test_x_dse.py."""
    base, name = os.path.split(path)
    for pattern, repl in ((".test.", "-dse.test."), (".spec.", "-dse.spec.")):
        if pattern in name:
            candidate = name.replace(pattern, repl, 1)
            break
    else:
        stem, ext = os.path.splitext(name)
        candidate = f"{stem}_dse{ext}"
    new_path = os.path.join(base, candidate) if base else candidate
    n = 2
    while new_path in existing or os.path.exists(os.path.join(workspace_dir, new_path)):
        new_path = new_path.replace("-dse.", f"-dse{n}.").replace("_dse.", f"_dse{n}.")
        n += 1
        if n > 5:
            break
    return new_path


def _restore_lockfile_churn(workspace_dir: str) -> list[str]:
    """Desfaz churn mecânico de lockfile ANTES do commit determinístico (2º
    disparo real: npm reescreveu 16 linhas de package-lock.json ao rodar os
    testes e o diff_budget reprovou a tarefa como mudança fora do plano).
    Regra: lockfile mudou mas o manifesto par (package.json…) NÃO mudou →
    restaura (modificado) ou remove (novo, untracked). Com o manifesto no
    diff a mudança é declarável e fica. Retorna os paths tratados."""
    import subprocess as _sp

    from dse_contracts.paths import lockfile_manifest_for

    try:
        porcelain = _sp.run(
            ["git", "status", "--porcelain"], cwd=workspace_dir,
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:  # noqa: BLE001 — best-effort; o L1 tem a mesma isenção
        return []
    status_by_path: dict[str, str] = {}
    for line in porcelain.splitlines():
        if len(line) > 3:
            status_by_path[line[3:].strip().strip('"')] = line[:2]
    restored: list[str] = []
    for rel, st in sorted(status_by_path.items()):
        manifest = lockfile_manifest_for(rel)
        if manifest is None or manifest in status_by_path:
            continue
        try:
            if st == "??":
                os.remove(os.path.join(workspace_dir, rel))
                restored.append(rel)
            else:
                proc = _sp.run(
                    ["git", "checkout", "--", rel], cwd=workspace_dir,
                    capture_output=True, text=True, timeout=30,
                )
                if proc.returncode == 0:
                    restored.append(rel)
        except OSError:
            continue
    return restored


def _restore_lockfile_churn_audited(
    workspace_dir: str, tenant_id: str, work_item_id: str, *, stage: str
) -> None:
    restored = _restore_lockfile_churn(workspace_dir)
    if restored:
        audit_emit(
            actor="system:sandbox-runtime",
            action="lockfile_churn_restored",
            tenant_id=tenant_id,
            work_item_id=work_item_id,
            details={"stage": stage, "restored": restored[:10]},
        )


def _tester_authored_files_in_history(workspace_dir: str) -> list[str]:
    """Test files de commits `tester(...)` anteriores ainda presentes no
    workspace. Se existem, o turno RE-RODA esses testes em vez de autorar
    novos (2º disparo real: cada ciclo de fix autorava MAIS um teste — o
    diff só crescia, o alvo mudava a cada volta e o loop nunca convergia).
    O fix cycle só funciona com alvo fixo."""
    import subprocess as _sp

    from dse_contracts.paths import is_test_path as _is_test

    try:
        log = _sp.run(
            ["git", "log", "--format=%H %s"], cwd=workspace_dir,
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:  # noqa: BLE001 — sem histórico legível, autora normalmente
        return []
    files: list[str] = []
    for line in log.splitlines():
        sha, _, subject = line.partition(" ")
        if not subject.startswith("tester("):
            continue
        try:
            names = _sp.run(
                ["git", "show", "--name-only", "--format=", sha], cwd=workspace_dir,
                capture_output=True, text=True, timeout=30,
            ).stdout
        except Exception:  # noqa: BLE001
            continue
        for rel in names.splitlines():
            rel = rel.strip()
            if (
                rel
                and _is_test(rel)
                and rel not in files
                and os.path.exists(os.path.join(workspace_dir, rel))
            ):
                files.append(rel)
    return files


_TEST_INFRA_ERROR_MARKERS = (
    "err_module_not_found", "cannot find package", "cannot find module",
    "err_require_esm", "syntaxerror", "modulenotfounderror", "importerror",
)


def _authored_test_infra_error(workspace_dir: str, test_paths: list[str]) -> str | None:
    """Roda SÓ os testes recém-autorados e detecta erro de INFRA (import/
    sintaxe — teste que nunca executa), distinto de asserção falhando (que é
    sinal legítimo de fix incompleto → fix cycle do Coder). Retorna o erro
    para re-autoria, ou None se os testes executam."""
    import subprocess as _sp

    if not test_paths:
        return None
    if test_paths[0].endswith(".py"):
        cmd = [sys.executable, "-m", "pytest", "-q", *test_paths]
    else:
        cmd = ["node", "--test", *test_paths]
    try:
        proc = _sp.run(cmd, cwd=workspace_dir, capture_output=True, text=True, timeout=180)
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"
    blob = (proc.stdout + proc.stderr).lower()
    if any(m in blob for m in _TEST_INFRA_ERROR_MARKERS):
        return (proc.stdout + proc.stderr)[-1500:]
    return None


@activity.defn(name=ACTIVITY_RUN_TESTER_TURN)
async def run_tester_turn(inp: RunTesterTurnInput) -> TesterTurnResult:
    reject_local_agent_execution("tester")
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

    # Autoria REAL (mesmo seletor do planner, P1 por config): sem script
    # explícito (testes) e com substrato real, o MODELO escreve os testes.
    # Falha em qualquer ponto → script vazio → tests_ran=False → gate para.
    #
    # Validação de INFRA com 1 re-autoria (achado do disparo real: o modelo
    # escreveu Jest num repo node:test — o teste nunca executava e o fix cycle
    # re-rodava o CODER, que nem pode tocar testes → loop sem saída): escreve,
    # roda SÓ os arquivos novos; erro de import/sintaxe → re-autora UMA vez com
    # o erro no prompt; persiste → remove os arquivos e devolve tests_ran=False
    # (o gate para limpo em vez de queimar turnos de Coder).
    if authoring_script is None and os.environ.get(SUBSTRATE_ENV_VAR, "fake").strip().lower() != "fake":
        # Idempotência do Tester (2º disparo real): testes já autorados em
        # ciclo anterior são RE-RODADOS, nunca re-autorados — o fix cycle
        # precisa de alvo fixo para o Coder convergir.
        reused = _tester_authored_files_in_history(workspace_dir)
        if reused:
            audit_emit(
                actor="system:sandbox-runtime",
                action="tester_reused_authored_tests",
                tenant_id=inp.tenant_id,
                work_item_id=inp.work_item_id,
                details={"test_files": reused[:10]},
            )
            authoring_script = [{"tool": "run_tests"}]
    if authoring_script is None and os.environ.get(SUBSTRATE_ENV_VAR, "fake").strip().lower() != "fake":
        error_feedback = ""
        for attempt in (1, 2):
            authoring_script = await run_sync_with_heartbeat(
                lambda _c: _model_authored_test_script(
                    inp, workspace_dir, headers, vk.virtual_key, error_feedback=error_feedback,
                ),
                None,
                stage=Stage.tester.value,
                work_item_id=inp.work_item_id,
                operation=f"tester_authoring_{attempt}",
            )
            if not authoring_script:
                break
            new_paths = [s["path"] for s in authoring_script if s.get("tool") == "write_file"]
            # escreve direto (mesmos paths que o toolset aceitaria — já filtrados)
            for s in authoring_script:
                if s.get("tool") == "write_file":
                    dest = os.path.join(workspace_dir, s["path"])
                    os.makedirs(os.path.dirname(dest) or workspace_dir, exist_ok=True)
                    with open(dest, "w") as fh:
                        fh.write(s["content"])
            infra_err = _authored_test_infra_error(workspace_dir, new_paths)
            if infra_err is None:
                # arquivos válidos: o loop de steps abaixo só precisa registrar
                # test_files + rodar a suíte (os writes já aconteceram).
                authoring_script = (
                    [{"tool": "write_file", "path": p, "content": open(os.path.join(workspace_dir, p)).read()} for p in new_paths]
                    + [{"tool": "run_tests"}]
                )
                break
            logger.warning("teste autorado com erro de INFRA (tentativa %d): %.200s", attempt, infra_err)
            for p in new_paths:  # remove o lixo antes de re-autorar/desistir
                try:
                    os.remove(os.path.join(workspace_dir, p))
                except OSError:
                    pass
            error_feedback = infra_err
            authoring_script = None
        if authoring_script is None and error_feedback:
            audit_emit(
                actor="system:sandbox-runtime",
                action="tester_authoring_invalid",
                tenant_id=inp.tenant_id,
                work_item_id=inp.work_item_id,
                details={"infra_error": error_feedback[:500]},
            )

    # Fase 3 (WSC-E3-T4b): o toolset é escopado ao work item — além de test
    # paths, `demos/<work_item_id>/` é escrita permitida (convenção do teste
    # `@demo`); `demos/` de OUTRO work item continua bloqueado.
    session = ScriptedAgentSession(
        toolset=TesterToolset(work_item_id=inp.work_item_id),
        workspace_dir=workspace_dir,
        retrieval=retrieval,
        tenant_id=inp.tenant_id,
        repo=inp.repo,
    )

    test_files: list[str] = []
    tests_ran = False
    tests_passed = False
    returncode = -1
    for index, step in enumerate(authoring_script or [], start=1):
        res = await run_sync_with_heartbeat(
            session.invoke,
            step["tool"],
            stage=Stage.tester.value,
            work_item_id=inp.work_item_id,
            operation=f"tester_tool_{index}_{step['tool']}",
            **{k: v for k, v in step.items() if k != "tool"},
        )
        if step["tool"] == "write_file":
            test_files.append(step["path"])
        if step["tool"] == "run_tests":
            tests_ran = True
            tests_passed = bool(res.detail.get("passed"))
            returncode = int(res.detail.get("returncode", -1))

    _restore_lockfile_churn_audited(workspace_dir, inp.tenant_id, inp.work_item_id, stage="tester")

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
# PROMOVIDO ao contrato (adendo 02 §2.3) e ENDURECIDO lá: a definição
# canônica em `dse_contracts.activities` agora tem `extra="forbid"` — tentar
# passar qualquer campo além de {work_item_id, tenant_id, plan, diff,
# task_class, data_class} (ex.: histórico do Coder) falha no DECODE da
# Activity, não apenas em teste. P3 estrutural na fundação.
from dse_contracts import RunL2ReviewInput  # noqa: E402


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
    reject_local_agent_execution("reviewer")
    return await _run_l2_review_impl(inp)


async def _run_l2_review_impl(inp: RunL2ReviewInput, *, verdict_fn=None) -> L2Verdict:
    """Constrói a sessão Reviewer de contexto FRESCO (WSC-E3-T5) e devolve o
    `L2Verdict`. A sessão recebe SÓ `ReviewerContext(plan, diff)` — nunca o
    histórico do Coder (P3). O veredito é RECOMENDAÇÃO (gateia progressão); o
    merge continua humano (P1)."""
    context = ReviewerContext(work_item_id=inp.work_item_id, plan=inp.plan, diff=inp.diff)
    session = FreshReviewerSession(context)
    verdict = await run_sync_with_heartbeat(
        session.review,
        verdict_fn or _default_reviewer_verdict,
        stage=Stage.reviewer.value,
        work_item_id=inp.work_item_id,
        operation="reviewer_verdict",
    )

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


# ===========================================================================
# Fase 4 — esteira de promoção de skill (WSC-E4-T3). Activities registradas
# como `@activity.defn` com os nomes/tipos de `dse_contracts.activities`
# (definidos no gate de entrada da Fase 4, antes do build). A lógica
# determinística vive em `skill_promotion` (P1); aqui só o wrapper Activity +
# tradução para os models de retorno do contrato.
# ===========================================================================
from dse_contracts import (  # noqa: E402
    ACTIVITY_EVAL_SKILL_CANDIDATE,
    ACTIVITY_PROMOTE_SKILL,
    EvalSkillCandidateInput,
    EvalSkillCandidateResult,
    PromoteSkillInput,
    PromoteSkillResult,
)

from . import skill_promotion  # noqa: E402


@activity.defn(name=ACTIVITY_EVAL_SKILL_CANDIDATE)
async def eval_skill_candidate(inp: EvalSkillCandidateInput) -> EvalSkillCandidateResult:
    """Replay do candidate contra o eval set histórico (positivos/negativos).
    Determinístico (P1) — produz um SCORE e as contagens, nunca uma decisão de
    promoção. `negative_regressions>0` ⇒ `passed=False`, o que bloqueia a
    transição candidate→approved por construção (o gate está em `promote_skill`).
    Grava em `skill_eval` (P8)."""
    outcome = skill_promotion.evaluate_candidate(
        inp.tenant_id, inp.skill_key, inp.candidate_version
    )
    return EvalSkillCandidateResult(
        skill_key=inp.skill_key,
        candidate_version=inp.candidate_version,
        passed=outcome.passed,
        score=outcome.score,
        positive_hits=outcome.positive_hits,
        negative_regressions=outcome.negative_regressions,
        detail=outcome.detail,
    )


@activity.defn(name=ACTIVITY_PROMOTE_SKILL)
async def promote_skill(inp: PromoteSkillInput) -> PromoteSkillResult:
    """Transição de estado GOVERNADA (candidate→approved→canary→active +
    rollback). P1/P3 NÃO-NEGOCIÁVEL: `to_status in {approved,active}` sem
    `approver` humano levanta `ApproverRequired` ANTES de qualquer escrita —
    promoção sem humano nomeado é impossível por construção (não há code path;
    a Activity propaga a exceção, o workflow do WS-B nunca "cai" numa promoção
    silenciosa). Toda transição → dse_audit.emit com a identidade do aprovador."""
    outcome = skill_promotion.promote(
        inp.tenant_id,
        inp.skill_key,
        inp.version,
        inp.to_status,
        approver=inp.approver,
        reason=inp.reason,
    )
    detail = ""
    if outcome.superseded_version is not None:
        detail = f"superseded v{outcome.superseded_version}"
    if outcome.restored_version is not None:
        detail = f"rollback: restored v{outcome.restored_version} to active"
    return PromoteSkillResult(
        skill_key=outcome.skill_key,
        version=outcome.version,
        from_status=outcome.from_status,
        to_status=outcome.to_status,
        ok=True,
        detail=detail,
    )


# Preflight no momento em que o worker importa/registra as Activities. Em
# produção, o adapter atual declara honestamente que ainda não executa stages
# dentro do sandbox; logo o worker recusa boot em vez de operar no fallback
# local. Em dev/test a compatibilidade existente é preservada.
validate_runtime_startup(
    isolated_stage_execution_available=(
        DEFAULT_SANDBOX_DRIVER.supports_isolated_stage_execution
    )
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
    run_planner_turn,
    run_tester_turn,
    run_l2_review,
    eval_skill_candidate,
    promote_skill,
]
