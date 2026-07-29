"""Temporal Activities for the sandbox lifecycle (WSC-E1-T3) + Coder session
(WSC-E3-T2). Exact names from `dse_contracts.activities` — imported by WS-B's
single worker (`services/orchestrator/worker.py`).

Defensive import: this module itself must never fail to import merely because a
heavy dependency is missing from the importer's venv — but since
`docker`/`temporalio`/`dse_contracts`/`dse_audit` are DECLARED dependencies of
this package (pyproject.toml), we import them directly here as usual. Anyone
importing this module without those dependencies installed must do so in THEIR
OWN try/except (the integrator's responsibility, see the docstring of
`sandbox_runtime/__init__.py`).

State between Activity calls: Temporal does not guarantee that a workflow's
Activity always runs on the same worker/process — which is why this module NEVER
keeps state in process memory across calls. All state lives:
  - in Docker (the sandbox container, found via the `dse.work_item_id` label);
  - on the filesystem, at deterministic paths derived from `work_item_id`
    (`_paths_for`) — the working workspace + the checkpoint bare repo.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("sandbox_runtime.activities")

# How much of a failed suite's output travels back to the Coder. Enough
# for a stack trace and the failing assertions; bounded because it ends up
# in a model prompt and in Temporal's history.
_FAILURE_OUTPUT_CHARS = 4000

from pydantic import BaseModel, Field
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
from .driver import (
    DEFAULT_SANDBOX_DRIVER,
    SandboxCheckpointRequest,
    SandboxProvisionRequest,
    SandboxRebuildRequest,
    select_sandbox_driver,
)
from .model_gateway_client import mint_virtual_key
from .remote_substrate import RemoteSubstrate
from .retrieval import RetrievalService
from .runtime_profile import (
    RuntimeProfile,
    reject_local_agent_execution,
    sandbox_inprocess_enabled,
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
from . import workspace_hygiene
from .substrate import SUBSTRATE_ENV_VAR, AgentSubstrate, substrate_from_env
from .toolsets import PlannerToolset, TesterToolset

_STATE_DIR = os.environ.get("DSE_SANDBOX_STATE_DIR", "/tmp/dse-sandboxes")


def _paths_for(work_item_id: str) -> tuple[str, str]:
    """Deterministic paths derived solely from the work_item_id — lets any
    worker, on any call, find the same workspace/bare repo without relying on
    in-memory state (see the module docstring)."""
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
    repo: str | None = None  # S4: target repo (e.g. "andre2654/fintex-wallet") to clone
    budget: dict[str, Any] = Field(default_factory=dict)
    image: str | None = None


@activity.defn(name=ACTIVITY_PROVISION_SANDBOX)
async def provision_sandbox(inp: ProvisionSandboxInput) -> SandboxHandle:
    profile = validate_runtime_startup()
    branch = inp.branch or _default_branch(inp.work_item_id)
    driver = select_sandbox_driver()
    workspace_dir, bare_repo_path = _paths_for(inp.work_item_id)

    if not driver.workspace_is_host_visible:
        # === K8s runtime: the workspace lives in a Pod volume (not visible to
        # the worker), so NOTHING is cloned/init'd on the host — the driver
        # brings up the Pod and the bootstrap clones the real repo INSIDE it,
        # through the egress-proxy; the repo token never enters the control
        # plane. Skills materialized on the host (the Docker path below) do NOT
        # reach the Pod — a known limitation of the K8s driver (in-pod skills is
        # a fast-follow). ===
        provisioned = driver.provision(
            SandboxProvisionRequest(
                work_item_id=inp.work_item_id,
                tenant_id=inp.tenant_id,
                branch=branch,
                workspace_path=workspace_dir,
                checkpoint_path=bare_repo_path,
                budget=inp.budget or {},
                repo=inp.repo,
                base_branch=inp.base_branch,
            )
        )
    else:
        is_new_checkpoint_repo = not Path(bare_repo_path).exists()
        if is_new_checkpoint_repo:
            git_checkpoint.provision_checkpoint_repo(bare_repo_path, branch)
        if not Path(workspace_dir).exists():
            # S4 (Phase 5): if the task has a target repo (e.g. github.com/andre2654/
            # fintex-wallet), CLONE the real code (with a token minted in the control
            # plane and scrubbed from the config) — the Coder works on the real repo.
            # With no repo/token (tests), fall back to the original empty-workspace
            # mechanics.
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
                    raise RuntimeError("SECURITY: token leaked into the workspace git config")
                if not cloned and profile is RuntimeProfile.production:
                    validate_runtime_profile(
                        local_fallback=(
                            f"clone of {inp.repo!r} failed/no credential and would fall back to an empty workspace"
                        )
                    )
            if not cloned:
                git_checkpoint.init_task_workspace(workspace_dir, bare_repo_path, branch, inp.base_branch)

        # Skills ticked for this repo (console → skill_registry.repo_scope, 0029)
        # are materialized HERE — after the clone, when the workspace is
        # guaranteed to be a git repo. Guidance is best-effort at provision time
        # (the Planner still fails cleanly if the registry goes down — the
        # mandatory read is its own); any skip is audited (P8).
        try:
            from .skill_files import materialize_skills as _materialize
            from .skill_registry import read_approved_skills as _read_skills
            _served = _read_skills(inp.tenant_id, repo=inp.repo)
            if not _served:
                # Keyed off the REGISTRY READ, not off the materialize result:
                # an empty _mat with a non-empty _served is the legitimate
                # "the repo already commits these skills" case, and the
                # "workspace has no .git yet" no-op. Only an empty READ means
                # the agent is running with no guidance at all — which is the
                # state this whole tenant was silently in, because an empty
                # list is a perfectly valid return and nothing said so.
                audit_emit(
                    actor="system:sandbox-runtime",
                    action="skills_resolved_empty",
                    tenant_id=inp.tenant_id,
                    work_item_id=inp.work_item_id,
                    details={"repo": inp.repo, "stage": "provision"},
                )
            _mat = _materialize(workspace_dir, _served)
            if _mat:
                audit_emit(
                    actor="system:sandbox-runtime",
                    action="skills_materialized",
                    tenant_id=inp.tenant_id,
                    work_item_id=inp.work_item_id,
                    details={"skills": _mat, "repo": inp.repo},
                )
        except Exception as exc:  # noqa: BLE001 — guidance never brings down the provision
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
    driver = select_sandbox_driver()
    branch = inp.branch or _default_branch(inp.work_item_id)
    workspace_dir, _bare_repo_path = _paths_for(inp.work_item_id)
    if not driver.workspace_is_host_visible:
        ref = driver.checkpoint(
            SandboxCheckpointRequest(
                work_item_id=inp.work_item_id, workspace_path=workspace_dir,
                branch=branch, phase=inp.phase,
            )
        )
    else:
        ref = git_checkpoint.checkpoint(inp.work_item_id, workspace_dir, branch, inp.phase)

    audit_emit(
        actor="system:sandbox-runtime",
        action="sandbox_checkpointed",
        tenant_id=inp.tenant_id,
        work_item_id=inp.work_item_id,
        details={"git_ref": ref.git_ref, "phase": ref.phase},
    )
    if not driver.workspace_is_host_visible:
        container_id = driver.sandbox_id_for(inp.work_item_id)
        resource_class = "small"
    else:
        existing = docker_driver.find_existing_container(inp.work_item_id)
        container_id = existing.id if existing else None
        resource_class = existing.labels.get("dse.resource_class", "small") if existing else "small"
    leases_store.record_lifecycle_event(
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        container_id=container_id,
        branch=branch,
        resource_class=resource_class,
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
    driver = select_sandbox_driver()
    branch = inp.branch or _default_branch(inp.work_item_id)
    old_workspace_dir, bare_repo_path = _paths_for(inp.work_item_id)

    if not driver.workspace_is_host_visible:
        # === K8s runtime: rebuild = a fresh Pod mounting the same checkpoint
        # volume (PVC); the bootstrap recovers the branch from the checkpoint.
        # With emptyDir (dev) the checkpoint dies with the Pod — the PVC is a
        # fast-follow. ===
        rebuild_result = driver.rebuild(
            SandboxRebuildRequest(
                provision=SandboxProvisionRequest(
                    work_item_id=inp.work_item_id,
                    tenant_id=inp.tenant_id,
                    branch=branch,
                    workspace_path=old_workspace_dir,
                    checkpoint_path=bare_repo_path,
                    budget=inp.budget or {},
                ),
                checkpoint_ref=inp.checkpoint_ref,
            )
        )
        provisioned = rebuild_result.sandbox
        recovered_sha = rebuild_result.recovered_sha
    else:
        # The old container may be dead (chaos) — remove it if it still exists
        # before recreating, so it does not collide with the new one's
        # name/labels.
        existing = docker_driver.find_existing_container(inp.work_item_id)
        if existing is not None:
            try:
                existing.remove(force=True)
            except Exception:  # noqa: BLE001 - the daemon may already have removed it
                pass

        # Fresh workspace (simulates losing the old container — it does not reuse
        # the previous working directory, only the checkpoint bare repo, which is
        # the durable source of truth).
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
    driver = select_sandbox_driver()
    resource_class = "small"
    runtime_minutes = 0.0
    if not driver.workspace_is_host_visible:
        # K8s runtime: delete the Pod. Pod lifetime is not tracked yet (the
        # metric stays 0.0 — fast-follow); the driver is fail-closed without a
        # cluster. container_id = the Pod name (Docker's `existing` does not
        # exist here).
        driver.teardown(driver.sandbox_id_for(inp.work_item_id))
        container_id = driver.sandbox_id_for(inp.work_item_id)
    else:
        existing = docker_driver.find_existing_container(inp.work_item_id)
        container_id = existing.id if existing else None
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
        container_id=container_id,
        branch=_default_branch(inp.work_item_id),
        resource_class=resource_class,
        status="torn_down",
    )


# ---------------------------------------------------------------------------
# run_coder_turn
# ---------------------------------------------------------------------------
# CANONICAL contract (anti-shadow — the same finding as in L1: the local model
# silently dropped fields the workflow sends, e.g. model_override and the new
# expected_files). Never redefine contract models locally.
from dse_contracts.activities import RunCoderTurnInput  # noqa: E402


def _build_substrate(script: list[dict[str, Any]] | None, *, stage: str = "coder") -> AgentSubstrate:
    """Substrate factory. Phase 3 (WSC-E3-T6): the choice is PER-DEPLOYMENT
    CONFIG — `DSE_CODER_SUBSTRATE` in {fake|openhands|claude-agent}, default
    `fake` (no gateway/SDK dependency has to be up for the tests). Swapping
    substrates never changes workflow code: WS-B keeps calling `run_coder_turn`
    by name, and this factory resolves the adapter behind the same
    `AgentSubstrate` interface.

    Phase 1 (plan 09): with `DSE_SANDBOX_INPROCESS=0` (and ALWAYS in production)
    the substrate becomes `RemoteSubstrate` — same substrate name, but the SDK
    executes INSIDE the sandbox via `SandboxDriver.execute_stage`; the worker
    only dispatches the typed contract (invariant 2 of the spec)."""
    if sandbox_inprocess_enabled():
        return substrate_from_env(script=script)
    return RemoteSubstrate(
        driver=select_sandbox_driver(),
        substrate_name=os.environ.get(SUBSTRATE_ENV_VAR, "fake").strip().lower(),
        stage=stage,
        fake_script=script,
    )


# Post-turn hygiene extracted to workspace_hygiene.py (Phase 1, plan 09): the
# same logic runs in the worker (Docker) and INSIDE the runner (K8s, --op
# post_turn) — single source of truth; the aliases preserve call sites/tests.
_prune_disposable_artifacts = workspace_hygiene.prune_disposable_artifacts
_revert_coder_test_edits = workspace_hygiene.revert_test_edits


@activity.defn(name=ACTIVITY_RUN_CODER_TURN)
async def run_coder_turn(inp: RunCoderTurnInput) -> CoderTurnResult:
    """Thin wrapper registered as the actual Activity — Temporal does not accept
    extra arguments (neither keyword-only nor optional positional) on functions
    decorated with `@activity.defn`. The real logic and the dependency-injection
    points for tests (`substrate`/`script`) live in `_run_coder_turn_impl`,
    called both from here (production, no overrides) and directly by the tests
    (with a scripted `FakeSubstrate`)."""
    if sandbox_inprocess_enabled():
        # Legacy in-process path: forbidden in production (fail-closed).
        reject_local_agent_execution("coder")
    else:
        # Isolated path (Phase 1): the SDK runs in the agent-runner inside the
        # sandbox; here we only validate real substrate/gateway per profile.
        validate_runtime_profile(require_real_substrate=True, require_real_gateway=True)
    return await _run_coder_turn_impl(inp)


async def _run_coder_turn_impl(
    inp: RunCoderTurnInput, substrate: AgentSubstrate | None = None, script: list[dict[str, Any]] | None = None
) -> CoderTurnResult:
    """Run one Coder turn inside the already-provisioned sandbox.

    P1 (no flow decision made by an LLM): the `substrate` ONLY edits files — the
    commit/push to the task branch happens here, in deterministic code
    (`ScopedGitSession`), never by the LLM. `substrate`/`script` are
    dependency-injection parameters used by the tests; in production WS-B's
    worker calls the `run_coder_turn` Activity without them and gets
    `FakeSubstrate` (document the real override via the env
    `DSE_CODER_SUBSTRATE=openhands` — see the README) until the OpenHands
    integration is complete.
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

    agent = substrate if substrate is not None else _build_substrate(script, stage=inp.stage)
    agent.create_session(
        work_item_id=inp.work_item_id,
        workspace_dir=workspace_dir,
        gateway_headers=headers,
        virtual_key=vk.virtual_key,
        gateway_base_url=vk.gateway_base_url,
    )

    # Anchor the plan into the instruction (found in the real run: the CLI
    # creates unsolicited reports — BUG_FIX_REPORT.md). Layer 1: an explicit
    # instruction; layer 2 (deterministic): a post-turn prune of disposable
    # artifacts ONLY (report/log/scratch), never of legitimate new source — see
    # _prune_disposable_artifacts.
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

    # Repo skills (console ticks materialized during the Planner turn + skills
    # committed in the target repo): ClaudeAgentSubstrate loads them via
    # setting_sources=["project"]; the note covers the other substrates.
    inp.instruction += workspace_skills_note(workspace_dir)

    pod_git = isinstance(agent, RemoteSubstrate) and not getattr(
        agent.driver, "workspace_is_host_visible", True
    )
    if pod_git:
        # K8s runtime: the workspace lives in the Pod volume — the turn's start
        # sha comes from a no-op checkpoint INSIDE the sandbox.
        base_sha = agent.checkpoint_sha(branch=branch, phase="turn-start")
    else:
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
        except Exception as exc:  # noqa: BLE001 — classification, not swallowing
            _raise_if_permanent_provider_error(exc)
            raise
        done = log.done
        turns += 1

    artifacts = agent.collect_artifacts()

    if pod_git:
        # K8s runtime: ALL the deterministic post-turn runs INSIDE the Pod
        # (--op post_turn — same sequence/source of truth as the block below, via
        # workspace_hygiene + scoped_git vendored into the runner). The audits
        # (P8) stay here in the worker, using the returned lists.
        post = agent.run_post_turn(
            branch=branch,
            expected_files=list(inp.expected_files or []),
            turn_start_sha=base_sha,
            commit_message=f"coder({inp.work_item_id}): {inp.instruction[:72]}",
        )
        for action, items in (
            ("coder_out_of_plan_files_pruned", post.pruned),
            ("coder_out_of_plan_files_kept", post.kept_out_of_plan),
            ("lockfile_churn_restored", post.restored_lockfiles),
            ("coder_test_edits_reverted", post.reverted_tests),
        ):
            if items:
                audit_emit(
                    actor="system:sandbox-runtime",
                    action=action,
                    tenant_id=inp.tenant_id,
                    work_item_id=inp.work_item_id,
                    details={"paths": items[:20]},
                )
        files_changed = list(post.files_changed)
    else:
        # Layer 2 (deterministic, P1): delete NEW (untracked) files that are
        # obvious CLI JUNK (unsolicited report/log/scratch) before the commit —
        # NOT "everything outside the plan" anymore (expected_files became
        # advisory in L1; a legitimate new source file outside the plan
        # SURVIVES). EXISTING files modified outside the plan stay — it is L1/the
        # budget that judges them.
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
                # Reconciliation observability (2026-07-22): under the old policy
                # these NEW out-of-plan files would be deleted; now they stay
                # (expected_files is advisory) and L1 is the judge (line budget +
                # forbidden_paths).
                audit_emit(
                    actor="system:sandbox-runtime",
                    action="coder_out_of_plan_files_kept",
                    tenant_id=inp.tenant_id,
                    work_item_id=inp.work_item_id,
                    details={"kept_out_of_plan": kept_out_of_plan[:20]},
                )

        _restore_lockfile_churn_audited(workspace_dir, inp.tenant_id, inp.work_item_id, stage="coder")

        # The Coder does NOT own the tests — the Tester authors them in ISOLATED
        # files (found in the real run on issue #1: the Coder edited the shared
        # seed in test/api.test.js and broke a pre-existing SIBLING test → the fix
        # cycle never converges, because fixing summary.js does not fix the test).
        # Revert ANY Coder change under test paths to the turn's starting state.
        reverted_tests = _revert_coder_test_edits(workspace_dir, base_sha)
        if reverted_tests:
            audit_emit(
                actor="system:sandbox-runtime",
                action="coder_test_edits_reverted",
                tenant_id=inp.tenant_id,
                work_item_id=inp.work_item_id,
                details={"reverted": reverted_tests[:20], "reason": "tests belong to the Tester stage"},
            )

        # Deterministic commit/push — the substrate never has git access.
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

    # Meter the coder into model_call_ledger. Every other stage lands there
    # through the gateway client, but the coder drives the bundled CLI with the
    # gateway configured by env, so its cost only ever existed on this result —
    # which is why the console's rollup, computed from the ledger alone,
    # reported $0.50 against $27.91 of real spend.
    #
    # The guard is the SUBSTRATE NAME, not the cost: FakeSubstrate reports
    # $0.01 per step, so a cost-based guard would start writing ledger rows from
    # four existing scripted tests into a table nothing is allowed to delete
    # from.
    ledger_id: int | None = None
    _scripted = getattr(agent, "substrate_name", "") == "fake" or type(agent).__name__ == "FakeSubstrate"
    if artifacts.cost_usd and not _scripted:
        try:
            from model_gateway_client.ledger import record_call

            ledger_id = record_call(
                tenant_id=inp.tenant_id,
                work_item_id=inp.work_item_id,
                stage=inp.stage,
                task_class=inp.task_class,
                model=os.environ.get("DSE_CODER_MODEL", "anthropic/claude"),
                cost_usd=artifacts.cost_usd,
                tokens_in=artifacts.tokens_in,
                tokens_out=artifacts.tokens_out,
            )
        except Exception as exc:  # noqa: BLE001 — never re-raise here
            # The commit and push above ALREADY happened. Raising would burn a
            # Temporal retry and re-spend real money to fix a bookkeeping miss.
            # Fail loud and fall back to the legacy audit-derived path.
            ledger_id = None
            logger.error(
                "coder cost ledger write FAILED (%s: %s); $%.4f falls back to the audit path",
                type(exc).__name__, str(exc)[:200], artifacts.cost_usd,
            )
            with contextlib.suppress(Exception):
                audit_emit(
                    actor="system:sandbox-runtime",
                    action="coder_cost_ledger_write_failed",
                    tenant_id=inp.tenant_id,
                    work_item_id=inp.work_item_id,
                    details={"error": type(exc).__name__, "cost_usd": artifacts.cost_usd,
                             "stage": inp.stage},
                )

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
            "ledger_id": ledger_id,
        },
    )

    return CoderTurnResult(
        ledger_id=ledger_id,
        sandbox_id=artifacts.sandbox_id,
        diff_summary=artifacts.diff_summary,
        files_changed=files_changed or artifacts.files_changed,
        cost_usd=artifacts.cost_usd,
        tokens_in=artifacts.tokens_in,
        tokens_out=artifacts.tokens_out,
    )


# ===========================================================================
# Phase 2 — stage-scoped sessions (WSC-E3-T3/T4/T5)
# ===========================================================================

# ---------------------------------------------------------------------------
# run_planner_turn (WSC-E3-T3) — read-only session, emits a PlanArtifact
# ---------------------------------------------------------------------------
# PROMOTED to the contract (addendum 02 §2.3, the Phase 3 entry gate): the
# canonical definition lives in `dse_contracts.activities` with boundary
# regression tests (packages/contracts/tests/test_activity_boundaries.py)
# validating WS-B's exact payloads. Re-imported for compatibility — every local
# consumer (tests, sessions) keeps working unchanged.
from dse_contracts import RunPlannerTurnInput  # noqa: E402


def _default_plan_proposer(ctx: PlannerContext, inp: "RunPlannerTurnInput") -> dict[str, Any]:
    """MINIMAL plan proposal when no real substrate is plugged in — a clearly
    flagged fixture (same spirit as the Coder's `FakeSubstrate`). With WS-B's
    anti-empty-PR guard (`planner_expected_files_empty_...`), a plan from this
    fixture ESCALATES at the gate — deliberate behavior: with no real model, DSE
    does not pretend to plan."""
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
    """The REAL repo tree on the base branch (best-effort, via the control
    plane's GitHub API) — without it the Planner guesses paths and
    plan_compliance rejects the real diff (found in the real run). On failure →
    empty list (the prompt degrades to 'likely paths')."""
    try:
        from dse_validation.config import GitHubConfig
        from dse_validation.github.client import build_github_client

        client = build_github_client(GitHubConfig())
        return client.get_tree_paths(repo, base_branch or "main")
    except Exception as exc:  # noqa: BLE001 — the tree is context, not a requirement
        logger.warning("repo tree unavailable for the planner (%s: %s)",
                       type(exc).__name__, str(exc)[:120])
        return []


def _model_plan_proposer(
    ctx: PlannerContext, inp: "RunPlannerTurnInput", headers: Any, virtual_key: str
) -> dict[str, Any] | None:
    """Plan proposed by the REAL MODEL through the gateway (stage=planner,
    virtual key, enforcement + cost ledger on the path — WSD). Returns None on
    any failure (missing import, refused call, invalid JSON) — the caller falls
    back to the fixture and WS-B's guard escalates CLEANLY (P6), never an
    invented plan.

    P1 preserved: the model only PROPOSES steps/expected_files/test_plan; risk
    and gates remain derived deterministically (classify_risk_class)."""
    try:
        from model_gateway_client.gateway_call import chat_completion
    except ImportError:
        logger.warning("model_gateway_client unavailable — planner falls back to the fixture")
        return None

    model = os.environ.get("DSE_PLANNER_MODEL") or os.environ.get("DSE_CODER_MODEL", "anthropic/claude")
    tree = _repo_tree_for_planner(inp.repo, inp.base_branch or "main")
    if tree:
        files_rule = "Use ONLY paths from the tree below (or new ones consistent with it)."
        tree_section = "\n## Repo tree (base branch)\n" + "\n".join(tree[:250])
    else:
        files_rule = "Propose the most likely paths per the ecosystem's conventions."
        tree_section = ""
    # The INSTRUCTION goes straight into the prompt (3rd real run: PlannerContext
    # does not carry the instruction — render() only has AGENTS.md/skills/repo
    # map, all empty for this tenant — and the model, never having SEEN the
    # issue, planned a generic wallet feature instead of the DELETE bug).
    prompt = _PLAN_PROMPT.format(
        instruction=(inp.instruction or "").strip()[:6000] or "(instruction missing)",
        # skill_body_chars=0 renders the skills as an INDEX (key, category,
        # title, path) instead of inlining their bodies. The Planner is a single
        # gateway chat_completion with a fixed 8 KB context budget, and render()
        # puts skills BEFORE the repo map and the untrusted block — so inlining
        # 21 real skills (~100 KB of body) delivered about one and a half of
        # them and silently evicted everything after. The Coder still gets the
        # full text: it reads .claude/skills/<key>/SKILL.md as files.
        context=ctx.render(skill_body_chars=0)[:8000],
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
    except Exception as exc:  # noqa: BLE001 — refusal/error => fixture (clean escalation)
        _raise_if_permanent_provider_error(exc)  # billing/auth: the right message on the issue
        logger.warning("planner via model failed (%s: %s) — fixture", type(exc).__name__, str(exc)[:200])
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
            raise ValueError("steps/expected_files are empty")
        return {
            "steps": steps[:10],
            "expected_files": files[:30],
            "test_plan": str(proposal.get("test_plan") or "Cover the change with tests (Tester turn)."),
        }
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("model plan did not parse (%s) — fixture; response: %.200s", exc, text)
        return None


@activity.defn(name=ACTIVITY_RUN_PLANNER_TURN)
async def run_planner_turn(inp: RunPlannerTurnInput) -> PlanArtifact:
    """Thin wrapper registered as the Temporal Activity (same pattern as
    `run_coder_turn`). The logic and the test injection points live in
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
    """READ-ONLY Planner session (WSC-E3-T3).

    Read-only toolset: hydrates AGENTS.md + the tenant's approved skill registry
    (E4) + CODEOWNERS + related tickets + retrieval/index (E5), and emits a
    structured PlanArtifact. Any WRITE tool fails (`ToolPermissionError`) — the
    session uses `PlannerToolset`. P1: `risk_class` is DERIVED by
    `classify_risk_class` (deterministic), not by the LLM's word — and it is what
    drives WS-B's gate.
    """
    workspace_dir, _bare = _paths_for(inp.work_item_id)

    # The model call (if any) goes out ONLY through the gateway, stage=planner.
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
    if not ctx.skills:
        # The emptiness was already ON the ledger — planner_turn_completed
        # carries skills_hydrated=[] — but buried in a details field, so it
        # could not be found with `WHERE action = …`. This makes the symmetric
        # provision-stage fact queryable at the Planner too.
        audit_emit(
            actor="system:sandbox-runtime",
            action="skills_resolved_empty",
            tenant_id=inp.tenant_id,
            work_item_id=inp.work_item_id,
            details={"repo": inp.repo, "stage": "planner", "task_class": inp.task_class},
        )

    # File-based skills (`.claude/skills/`) are materialized by
    # provision_sandbox (after the clone — the Planner may run BEFORE the
    # provision, with the workspace not yet existing here). This re-materialize
    # is a no-op in that case (the `.git` guard in skill_files) and refreshes the
    # workspace when the registry changed between retries.
    skills_materialized = materialize_skills(workspace_dir, ctx.skills)

    # Read-only session: any write step in the exploration_script FAILS here
    # (the planner toolset), which is exactly the conformance test.
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

    # Proposer selection (P1 — by CONFIG, never by a model):
    #   explicit proposer (tests) > real model (substrate != fake) with a
    #   fallback to the fixture > fixture. The fixture has empty expected_files
    #   and WS-B's guard escalates — deliberate when there is no model.
    if proposer is not None:
        proposal_fn = proposer
    elif os.environ.get(SUBSTRATE_ENV_VAR, "fake").strip().lower() != "fake":
        def proposal_fn(c):  # noqa: ANN001 — run_sync_with_heartbeat's signature
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
# run_tester_turn (WSC-E3-T4) — test runners + test authoring (test paths only)
# ---------------------------------------------------------------------------
# PROMOTED to the contract (addendum 02 §2.3) — canonical definition in
# `dse_contracts.activities`, boundary tests in the foundation. Re-imported for
# the local consumers' compatibility.
from dse_contracts import RunTesterTurnInput, TesterTurnResult  # noqa: E402


# PERMANENT provider error patterns (found in the real 2026-07-22 run): exhausted
# credits/an invalid key are not transient — retrying is an infinite loop of
# attempts. Raised non_retryable; the workflow converts them into _FailClosed →
# a clean failure commented on the issue (P6).
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
        from dse_contracts.failure import FailureClass, failure_type
        from temporalio.exceptions import ApplicationError

        # Phase 2 (plan 09): the class travels in the TYPE (the contract's closed
        # vocabulary) — the workflow no longer depends on a message substring.
        # (The legacy "ProviderBillingError" is still recognized on parse for
        # replay.)
        raise ApplicationError(
            f"provider_billing_or_auth: {str(exc)[:200]}",
            type=failure_type(FailureClass.provider_billing),
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
    """Deterministic context so authoring imitates the REAL repo (found in the
    real run: the model wrote Jest in a node:test repo with no deps and also
    overwrote the original test — nothing ever ran). Returns
    (package_json, example_test, existing_test_paths)."""
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
    """Test authoring by the REAL MODEL (same pattern as the planner): 1
    stage=tester call through the gateway → test files → deterministic script
    [write_file..., run_tests]. Deterministic guard-rails (P1):
      - IMITATION context: package.json + one existing test from the repo (the
        model copies the real runner, it never invents jest/supertest);
      - paths outside test paths OR pointing at ALREADY EXISTING tests are
        rejected (overwriting a repo test would destroy the suite);
      - `error_feedback` re-injects the infra error from the 1st attempt (1
        retry).
    Any failure → None → tests_ran=False → WS-B's gate stops cleanly."""
    try:
        from model_gateway_client.gateway_call import chat_completion
    except ImportError:
        logger.warning("model_gateway_client unavailable — tester without real authoring")
        return None
    from dse_contracts.paths import is_test_path

    diff = ""
    try:
        import subprocess as _sp
        proc = _sp.run(["git", "show", "--stat", "-p", "HEAD"],
                       cwd=workspace_dir, capture_output=True, text=True, timeout=30)
        diff = proc.stdout[-8000:]
    except Exception:  # noqa: BLE001 — the diff is context, not a requirement
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
    # Repo skills (materialized by the Planner + committed in the target repo):
    # the Tester must follow the guidance too (test style, tenant conventions).
    prompt += workspace_skills_note(workspace_dir)[:2000]
    try:
        result = chat_completion(
            headers=headers, virtual_key=virtual_key, model=model,
            messages=[{"role": "user", "content": prompt}],
            # 8000: found in the real run with Haiku — 4000 truncated the JSON in
            # the middle of the content ("Unterminated string") and the whole
            # authoring pass fell over.
            timeout=180.0, max_tokens=8000, temperature=0,
        )
    except Exception as exc:  # noqa: BLE001
        _raise_if_permanent_provider_error(exc)  # billing/auth: the right message on the issue
        logger.warning("tester via model failed (%s: %s)", type(exc).__name__, str(exc)[:200])
        return None

    text = (result.content or "").strip()
    if text.startswith("```"):
        text = text.strip("`\n")
        text = text[4:] if text.startswith("json") else text
    try:
        parsed, _ = json.JSONDecoder().raw_decode(text.strip())
        files = parsed.get("files") or []
    except json.JSONDecodeError as exc:
        logger.warning("test authoring did not parse (%s); response: %.200s", exc, text)
        return None

    script: list[dict[str, Any]] = []
    for f in files[:3]:
        path, content = str(f.get("path") or ""), str(f.get("content") or "")
        if not (path and content and is_test_path(path)):
            logger.warning("test path refused (outside the allowed test paths): %r", path)
            continue
        if path in existing_tests:
            # Instead of discarding it (which left the script empty whenever the
            # model insisted on the existing test), RENAME deterministically to a
            # new file in the SAME directory — relative imports stay intact.
            renamed = _dedupe_test_path(path, existing_tests, workspace_dir)
            logger.warning("test path ALREADY EXISTS — renamed %r → %r", path, renamed)
            path = renamed
        script.append({"tool": "write_file", "path": path, "content": content})
    if not script:
        return None
    script.append({"tool": "run_tests"})
    return script


def _dedupe_test_path(path: str, existing: set[str], workspace_dir: str) -> str:
    """A new name in the same directory that still matches is_test_path:
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


_restore_lockfile_churn = workspace_hygiene.restore_lockfile_churn
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
    """Test files from previous `tester(...)` commits that are still present in
    the workspace. If they exist, the turn RE-RUNS those tests instead of
    authoring new ones (2nd real run: every fix cycle authored ONE MORE test —
    the diff only grew, the target moved on each lap and the loop never
    converged). The fix cycle only works with a fixed target."""
    import subprocess as _sp

    from dse_contracts.paths import is_test_path as _is_test

    try:
        log = _sp.run(
            ["git", "log", "--format=%H %s"], cwd=workspace_dir,
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:  # noqa: BLE001 — with no readable history, author as usual
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
    """Run ONLY the freshly authored tests and detect an INFRA error
    (import/syntax — a test that never executes), as distinct from a failing
    assertion (which is a legitimate signal of an incomplete fix → the Coder's
    fix cycle). Returns the error for re-authoring, or None if the tests
    execute."""
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


def _tester_pod_sync(
    inp: "RunTesterTurnInput",
    sandbox_id: str,
    headers: Any,
    virtual_key: str,
    push: bool,
) -> TesterTurnResult:
    """SYNCHRONOUS body of the Tester bridge on the K8s runtime (run under
    heartbeat).

    It operates INSIDE the sandbox Pod via `kubectl exec` — the workspace and the
    toolchain (node/git) live in the Pod (the Coder already ran there), NOT in the
    worker. Authoring (the model) needs the workspace context: `kubectl cp` pulls
    a READ-ONLY local clone to feed `_model_authored_test_script`; the writes, the
    suite run and the test commit happen in the Pod. Simplification versus the
    Docker/dev path: no infra-error loop and no re-run idempotency (1 authoring
    pass; empty authoring → tests_ran=False → the gate stops cleanly)."""
    import shlex as _shlex
    import shutil as _shutil
    import subprocess as _sp
    import tempfile as _tf

    ns = os.environ.get("DSE_SANDBOX_K8S_NAMESPACE", "dse-sandboxes")
    kubectl = os.environ.get("DSE_KUBECTL", "kubectl")
    kctx = os.environ.get("DSE_SANDBOX_KUBE_CONTEXT", "")
    kbase = [kubectl] + (["--context", kctx] if kctx else [])

    def _pod_sh(script: str, *, timeout: int = 600, input_text: str | None = None) -> "_sp.CompletedProcess":
        return _sp.run(
            kbase + ["exec", "-i", sandbox_id, "-n", ns, "--", "sh", "-c", script],
            capture_output=True, text=True, timeout=timeout, input=input_text,
        )

    # git identity IN THE POD (where the repo actually is — the local path's
    # "git config exit 128" was exactly this running in the worker, which does
    # not have the workspace).
    _pod_sh("cd /workspace && git config user.name dse-tester && git config user.email tester@dse.local")

    # idempotency (retry): if a previous Tester round already authored tests
    # (a `tester(...)` commit) and they still exist in the Pod, RE-RUN instead of
    # re-authoring — a FIXED target so the Coder can converge (the local path
    # does the same with _tester_authored_files_in_history). Without this, each
    # retry authors a new file, the target moves and the Coder↔Tester loop never
    # closes.
    reused = [
        f for f in _pod_sh(
            "cd /workspace && git log --pretty=%H --grep='^tester(' -n 8 2>/dev/null | "
            'while read h; do git show --name-only --pretty=format: "$h" 2>/dev/null; done | '
            'sort -u | while read f; do [ -n "$f" ] && [ -f "$f" ] && echo "$f"; done'
        ).stdout.splitlines() if f.strip()
    ]

    test_files: list[str] = []
    authored_new = False
    if reused:
        test_files = reused
        logger.info("tester k8s: reusing %d test(s) authored in a previous round", len(reused))
    else:
        # REAL authoring: workspace context (git show HEAD + package.json + an
        # example test). kubectl cp the Pod's /workspace → a read-only local clone.
        authoring_script: list[dict[str, Any]] | None = None
        tmp = _tf.mkdtemp(prefix="dse-tester-k8s-")
        try:
            local_ws = os.path.join(tmp, "ws")
            cp = _sp.run(
                kbase + ["cp", f"{ns}/{sandbox_id}:/workspace", local_ws],
                capture_output=True, text=True, timeout=180,
            )
            if cp.returncode == 0 and os.path.isdir(os.path.join(local_ws, ".git")):
                authoring_script = _model_authored_test_script(inp, local_ws, headers, virtual_key)
            else:
                logger.warning("tester k8s: kubectl cp of the workspace failed (rc=%s): %.200s",
                               cp.returncode, (cp.stderr or "")[:200])
        finally:
            _shutil.rmtree(tmp, ignore_errors=True)

        # write the test files IN THE POD (content via stdin — no quoting of the content)
        for s in (authoring_script or []):
            if s.get("tool") != "write_file":
                continue
            path, content = str(s.get("path") or ""), str(s.get("content") or "")
            if not path:
                continue
            w = _pod_sh(
                f'cd /workspace && mkdir -p "$(dirname {_shlex.quote(path)})" && cat > {_shlex.quote(path)}',
                input_text=content,
            )
            if w.returncode == 0:
                test_files.append(path)
                authored_new = True
            else:
                logger.warning("tester k8s: failed writing %s into the Pod: %.200s", path, (w.stderr or "")[:200])

    # run the suite IN THE POD (deterministic detection: package.json with a
    # "test" script → npm test; otherwise pytest). node/npm/pytest come from the
    # image (rebuild).
    run = _pod_sh(
        'cd /workspace && '
        'if [ -f package.json ] && grep -q \'"test"\' package.json; then '
        'npm install --no-audit --no-fund >/dev/null 2>&1 || true; npm test --silent; '
        'else python3 -m pytest -q; fi',
        timeout=600,
    )
    tests_ran = bool(test_files)
    tests_passed = tests_ran and run.returncode == 0
    returncode = run.returncode
    # Kept, not just logged. The workflow feeds this back to the Coder on the
    # next attempt; without it the retry repeats the original instruction
    # verbatim and the same test fails again. Tail, because the useful part of a
    # test runner's output is the end.
    failure_output = ""
    if tests_ran and not tests_passed:
        failure_output = ((run.stdout or "") + (run.stderr or ""))[-_FAILURE_OUTPUT_CHARS:]
        logger.warning("tester k8s: suite failed (rc=%s): %.300s", returncode, failure_output[-300:])

    # commit the test files IN THE POD (current branch; the post-tester checkpoint
    # picks them up and finalize pushes). No push here.
    head_sha = None
    if authored_new and test_files:
        files_arg = " ".join(_shlex.quote(p) for p in test_files)
        msg = f"tester({inp.work_item_id}): {(inp.instruction or '')[:60]}"
        _pod_sh(
            f"cd /workspace && git add {files_arg} && git commit -m {_shlex.quote(msg)} || true",
            timeout=120,
        )
    h = _pod_sh("cd /workspace && git rev-parse HEAD", timeout=60)
    head_sha = (h.stdout or "").strip() or None

    audit_emit(
        actor="system:sandbox-runtime",
        action="tester_turn_completed",
        tenant_id=inp.tenant_id,
        work_item_id=inp.work_item_id,
        details={
            "stage": "tester", "runtime": "k8s", "test_files": test_files,
            "tests_ran": tests_ran, "tests_passed": tests_passed, "returncode": returncode,
        },
    )
    return TesterTurnResult(
        sandbox_id=inp.work_item_id,
        test_files=test_files,
        tests_ran=tests_ran,
        tests_passed=tests_passed,
        returncode=returncode,
        head_sha=head_sha,
        cost_usd=0.0,
        failure_output=failure_output,
    )


async def _run_tester_turn_pod(
    inp: "RunTesterTurnInput", *, sandbox_id: str, headers: Any, virtual_key: str, push: bool = True,
) -> TesterTurnResult:
    """Tester on the K8s runtime (Pod). Runs the synchronous body under heartbeat
    — the model's authoring + `npm install`/`npm test` can take minutes."""
    return await run_sync_with_heartbeat(
        lambda _c: _tester_pod_sync(inp, sandbox_id, headers, virtual_key, push),
        None,
        stage=Stage.tester.value,
        work_item_id=inp.work_item_id,
        operation="tester_pod_bridge",
    )


async def _run_tester_turn_impl(
    inp: RunTesterTurnInput,
    *,
    retrieval: RetrievalService | None = None,
    authoring_script: list[dict[str, Any]] | None = None,
    push: bool = True,
) -> TesterTurnResult:
    """Tester session (WSC-E3-T4): test authoring + runners. Edits are allowed
    ONLY under test paths (`TesterToolset` refuses writes outside them). The
    written tests actually EXECUTE (`run_tests` → real pytest in the workspace),
    they are not merely generated. The commit/push of the test files is
    deterministic (`ScopedGitSession`), never done by the LLM (P1)."""
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

    # K8s runtime: the workspace lives INSIDE the Pod (the Coder ran there via
    # RemoteSubstrate), not on the worker's FS. The local path below
    # (ScopedGitSession/ScriptedAgentSession) assumes a host-visible workspace and
    # breaks on K8s (git config exit 128, no workspace). Route to the bridge that
    # operates INSIDE the Pod via kubectl exec.
    try:
        _driver = select_sandbox_driver()
        _host_visible = _driver.workspace_is_host_visible
    except Exception:  # noqa: BLE001 — no resolvable driver → keep the local path
        _driver, _host_visible = None, True
    if _driver is not None and not _host_visible:
        return await _run_tester_turn_pod(
            inp,
            sandbox_id=_driver.sandbox_id_for(inp.work_item_id),
            headers=headers,
            virtual_key=vk.virtual_key,
            push=push,
        )

    # REAL authoring (same selector as the planner, P1 by config): with no
    # explicit script (tests) and a real substrate, the MODEL writes the tests.
    # A failure at any point → empty script → tests_ran=False → the gate stops.
    #
    # INFRA validation with 1 re-authoring pass (found in the real run: the model
    # wrote Jest in a node:test repo — the test never executed and the fix cycle
    # kept re-running the CODER, which cannot even touch tests → a loop with no
    # exit): write, run ONLY the new files; an import/syntax error → re-author
    # ONCE with the error in the prompt; if it persists → remove the files and
    # return tests_ran=False (the gate stops cleanly instead of burning Coder
    # turns).
    if authoring_script is None and os.environ.get(SUBSTRATE_ENV_VAR, "fake").strip().lower() != "fake":
        # Tester idempotency (2nd real run): tests already authored in a previous
        # cycle are RE-RUN, never re-authored — the fix cycle needs a fixed target
        # for the Coder to converge.
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
            # write directly (the same paths the toolset would accept — already filtered)
            for s in authoring_script:
                if s.get("tool") == "write_file":
                    dest = os.path.join(workspace_dir, s["path"])
                    os.makedirs(os.path.dirname(dest) or workspace_dir, exist_ok=True)
                    with open(dest, "w") as fh:
                        fh.write(s["content"])
            infra_err = _authored_test_infra_error(workspace_dir, new_paths)
            if infra_err is None:
                # valid files: the step loop below only needs to register
                # test_files + run the suite (the writes already happened).
                authoring_script = (
                    [{"tool": "write_file", "path": p, "content": open(os.path.join(workspace_dir, p)).read()} for p in new_paths]
                    + [{"tool": "run_tests"}]
                )
                break
            logger.warning("authored test hit an INFRA error (attempt %d): %.200s", attempt, infra_err)
            for p in new_paths:  # remove the junk before re-authoring/giving up
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

    # Phase 3 (WSC-E3-T4b): the toolset is scoped to the work item — besides test
    # paths, `demos/<work_item_id>/` is an allowed write (the `@demo` test
    # convention); ANOTHER work item's `demos/` stays blocked.
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
    failure_output = ""
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
            if not tests_passed:
                # Same reason as the k8s path: the retry is useless without it.
                # Whichever of these the toolset filled is what the Coder gets.
                raw = (
                    res.detail.get("output")
                    or (str(res.detail.get("stdout") or "") + str(res.detail.get("stderr") or ""))
                )
                failure_output = str(raw)[-_FAILURE_OUTPUT_CHARS:]

    _restore_lockfile_churn_audited(workspace_dir, inp.tenant_id, inp.work_item_id, stage="tester")

    # Deterministic commit/push of the test files (only test paths were written —
    # the toolset guaranteed it). Git escapes live in the code, never in the LLM.
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
        failure_output=failure_output,
    )


# ---------------------------------------------------------------------------
# run_l2_review (WSC-E3-T5) — fresh-context Reviewer session, returns L2Verdict
# ---------------------------------------------------------------------------
# PROMOTED to the contract (addendum 02 §2.3) and HARDENED there: the canonical
# definition in `dse_contracts.activities` now has `extra="forbid"` — trying to
# pass any field beyond {work_item_id, tenant_id, plan, diff, task_class,
# data_class} (e.g. the Coder's history) fails at the Activity's DECODE, not
# just in a test. Structural P3 in the foundation.
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
    # dedup preserving order
    seen: set[str] = set()
    out = []
    for f in files:
        if f and f not in seen:
            seen.add(f)
            out.append(f)
    return out


def _default_reviewer_verdict(ctx: ReviewerContext):
    """Deterministic STAND-IN reviewer (a clearly flagged fixture, same spirit as
    FakeSubstrate). Judges the diff's adherence to the plan by objective rules:
    (a) no file changed outside the declared blast radius (`expected_files`, when
    non-empty); (b) no file under `forbidden_paths`. In production a FRESH
    OpenHands session (plan+diff only) replaces this and returns
    convention/logic objections with file/line — override via
    `_run_l2_review_impl(..., verdict_fn=...)`. See the README."""
    changed = _changed_files_from_diff(ctx.diff)
    objections: list[str] = []
    expected = set(ctx.plan.expected_files)
    for f in changed:
        if expected and f not in expected:
            objections.append(f"{f}: changed outside the blast radius declared in the plan (expected_files)")
        for fb in ctx.plan.forbidden_paths:
            if f.startswith(fb.rstrip("*")):
                objections.append(f"{f}: touches forbidden_path '{fb}' — requires the human path")
    return (len(objections) == 0, objections, 0.0)


@activity.defn(name=ACTIVITY_RUN_L2_REVIEW)
async def run_l2_review(inp: RunL2ReviewInput) -> L2Verdict:
    reject_local_agent_execution("reviewer")
    return await _run_l2_review_impl(inp)


async def _run_l2_review_impl(inp: RunL2ReviewInput, *, verdict_fn=None) -> L2Verdict:
    """Build the FRESH-context Reviewer session (WSC-E3-T5) and return the
    `L2Verdict`. The session receives ONLY `ReviewerContext(plan, diff)` — never
    the Coder's history (P3). The verdict is a RECOMMENDATION (it gates
    progression); the merge remains human (P1)."""
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
# Phase 4 — skill promotion pipeline (WSC-E4-T3). Activities registered as
# `@activity.defn` with the names/types from `dse_contracts.activities` (defined
# at the Phase 4 entry gate, before the build). The deterministic logic lives in
# `skill_promotion` (P1); here we only have the Activity wrapper + the
# translation into the contract's return models.
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
    """Replay the candidate against the historical eval set
    (positives/negatives). Deterministic (P1) — it produces a SCORE and the
    counts, never a promotion decision. `negative_regressions>0` ⇒
    `passed=False`, which blocks the candidate→approved transition by
    construction (the gate is in `promote_skill`). Writes to `skill_eval` (P8)."""
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
    """GOVERNED state transition (candidate→approved→canary→active + rollback).
    NON-NEGOTIABLE P1/P3: `to_status in {approved,active}` without a human
    `approver` raises `ApproverRequired` BEFORE any write — promotion without a
    named human is impossible by construction (there is no code path; the
    Activity propagates the exception and WS-B's workflow never "falls" into a
    silent promotion). Every transition → dse_audit.emit with the approver's
    identity."""
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


# Preflight at the moment the worker imports/registers the Activities. In
# production, the current adapter honestly declares that it does not yet execute
# stages inside the sandbox; so the worker refuses to boot instead of operating
# on the local fallback. In dev/test the existing compatibility is preserved.
validate_runtime_startup(
    isolated_stage_execution_available=(
        DEFAULT_SANDBOX_DRIVER.supports_isolated_stage_execution
    )
)


# Consumed by the single worker's defensive loader (services/orchestrator/
# src/dse_orchestrator/worker.py:_load_cross_workstream_activities) — the name
# `ACTIVITIES` and the contract the integrator expects (see its docstring).
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
