"""Regressões do perfil production fail-closed do sandbox-runtime."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys

import pytest

from dse_contracts import GatewayCallHeaders, PlanArtifact, Stage
from sandbox_runtime.activities import (
    ProvisionSandboxInput,
    RunCoderTurnInput,
    RunL2ReviewInput,
    RunPlannerTurnInput,
    RunTesterTurnInput,
    run_coder_turn,
    run_l2_review,
    run_planner_turn,
    run_tester_turn,
    provision_sandbox,
)
from sandbox_runtime.model_gateway_client import mint_virtual_key
from sandbox_runtime.runtime_profile import (
    MODEL_GATEWAY_FIXTURE_ENV_VAR,
    PROFILE_ENV_VAR,
    SANDBOX_INPROCESS_ENV_VAR,
    SUBSTRATE_ENV_VAR,
    RuntimeProfile,
    RuntimeProfileViolation,
    current_runtime_profile,
    validate_runtime_startup,
)
from sandbox_runtime.substrate import FakeSubstrate, substrate_from_env


def _production(monkeypatch) -> None:
    monkeypatch.setenv(PROFILE_ENV_VAR, "production")
    monkeypatch.setenv(SANDBOX_INPROCESS_ENV_VAR, "0")
    monkeypatch.setenv(SUBSTRATE_ENV_VAR, "claude-agent")
    monkeypatch.setenv(MODEL_GATEWAY_FIXTURE_ENV_VAR, "0")


def test_default_profile_preserves_dev_compatibility(monkeypatch):
    monkeypatch.delenv(PROFILE_ENV_VAR, raising=False)
    monkeypatch.delenv(SUBSTRATE_ENV_VAR, raising=False)
    assert current_runtime_profile() is RuntimeProfile.dev
    assert isinstance(substrate_from_env(script=[]), FakeSubstrate)


@pytest.mark.parametrize("alias", ["production", "prod", "pilot"])
def test_production_aliases_select_restrictive_profile(monkeypatch, alias):
    monkeypatch.setenv(PROFILE_ENV_VAR, alias)
    assert current_runtime_profile() is RuntimeProfile.production


def test_unknown_profile_fails_instead_of_falling_back_to_dev(monkeypatch):
    monkeypatch.setenv(PROFILE_ENV_VAR, "prodution")
    with pytest.raises(RuntimeProfileViolation, match="desconhecido"):
        current_runtime_profile()


@pytest.mark.parametrize("enabled", ["1", "true", "yes", "on"])
def test_production_rejects_every_truthy_inprocess_spelling(monkeypatch, enabled):
    _production(monkeypatch)
    monkeypatch.setenv(SANDBOX_INPROCESS_ENV_VAR, enabled)
    with pytest.raises(RuntimeProfileViolation, match="DSE_SANDBOX_INPROCESS"):
        validate_runtime_startup()


@pytest.mark.parametrize("substrate", ["", "fake", "fixture", "desconhecido"])
def test_production_rejects_fake_or_empty_substrate(monkeypatch, substrate):
    _production(monkeypatch)
    monkeypatch.setenv(SUBSTRATE_ENV_VAR, substrate)
    with pytest.raises(RuntimeProfileViolation, match="não é um substrato real"):
        validate_runtime_startup()


def test_direct_factory_cannot_select_fake_in_production(monkeypatch):
    _production(monkeypatch)
    with pytest.raises(RuntimeProfileViolation, match="substrato real"):
        substrate_from_env("fake", script=[])


@pytest.mark.parametrize("fixture", [None, "1", "true", "yes", "on"])
def test_production_rejects_gateway_fixture_including_legacy_default(monkeypatch, fixture):
    _production(monkeypatch)
    if fixture is None:
        monkeypatch.delenv(MODEL_GATEWAY_FIXTURE_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(MODEL_GATEWAY_FIXTURE_ENV_VAR, fixture)
    with pytest.raises(RuntimeProfileViolation, match="virtual key fixture"):
        validate_runtime_startup()


def test_safe_static_production_configuration_passes_preflight(monkeypatch):
    _production(monkeypatch)
    assert validate_runtime_startup() is RuntimeProfile.production


def test_worker_boot_in_production_requires_isolated_execution_semantics():
    """Fase 1 (plano 09): com o driver suportando execute_stage isolado
    (docker exec/kubectl exec → agent-runner), o boot em produção com config
    segura é PERMITIDO — e continua recusado se alguém religar o in-process."""
    base = {
        PROFILE_ENV_VAR: "production",
        SUBSTRATE_ENV_VAR: "claude-agent",
        MODEL_GATEWAY_FIXTURE_ENV_VAR: "0",
    }

    env_ok = os.environ.copy()
    env_ok.update(base | {SANDBOX_INPROCESS_ENV_VAR: "0"})
    result = subprocess.run(
        [sys.executable, "-c", "import sandbox_runtime.activities"],
        env=env_ok, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr

    env_inprocess = os.environ.copy()
    env_inprocess.update(base | {SANDBOX_INPROCESS_ENV_VAR: "1"})
    result = subprocess.run(
        [sys.executable, "-c", "import sandbox_runtime.activities"],
        env=env_inprocess, capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "execução fora do sandbox" in result.stderr


def test_production_coder_is_isolated_never_inprocess(monkeypatch):
    """Fase 1 (plano 09): em produção o turno do Coder SÓ existe isolado —
    a fábrica devolve RemoteSubstrate (o SDK nunca roda no worker) e religar
    o in-process por flag continua recusado."""
    from sandbox_runtime.activities import _build_substrate
    from sandbox_runtime.remote_substrate import RemoteSubstrate

    _production(monkeypatch)
    assert isinstance(_build_substrate(None, stage="coder"), RemoteSubstrate)

    monkeypatch.setenv(SANDBOX_INPROCESS_ENV_VAR, "1")
    inp = RunCoderTurnInput(work_item_id="wi-prod", tenant_id="tenant-prod", instruction="edite")
    with pytest.raises(RuntimeProfileViolation, match="execução fora do sandbox"):
        asyncio.run(run_coder_turn(inp))


@pytest.mark.parametrize(
    ("stage", "call"),
    [
        (
            "planner",
            lambda: run_planner_turn(
                RunPlannerTurnInput(
                    work_item_id="wi-prod",
                    tenant_id="tenant-prod",
                    instruction="planeje",
                )
            ),
        ),
        (
            "tester",
            lambda: run_tester_turn(
                RunTesterTurnInput(
                    work_item_id="wi-prod",
                    tenant_id="tenant-prod",
                    instruction="teste",
                )
            ),
        ),
        (
            "reviewer",
            lambda: run_l2_review(
                RunL2ReviewInput(
                    work_item_id="wi-prod",
                    tenant_id="tenant-prod",
                    plan=PlanArtifact(work_item_id="wi-prod"),
                    diff="",
                )
            ),
        ),
    ],
)
def test_all_current_stage_standins_are_blocked_in_production(monkeypatch, stage, call):
    _production(monkeypatch)
    with pytest.raises(RuntimeProfileViolation, match=rf"estágio '{stage}'.*worker"):
        asyncio.run(call())


def test_gateway_client_never_attempts_fixture_in_production(monkeypatch):
    _production(monkeypatch)
    monkeypatch.setenv(MODEL_GATEWAY_FIXTURE_ENV_VAR, "1")
    headers = GatewayCallHeaders(
        tenant_id="tenant-prod",
        work_item_id="wi-prod",
        stage=Stage.coder,
    )
    with pytest.raises(RuntimeProfileViolation, match="virtual key fixture"):
        mint_virtual_key(headers, gateway_base_url="http://127.0.0.1:1", timeout_s=0.01)


def test_repo_clone_failure_cannot_fall_back_to_empty_workspace_in_production(
    monkeypatch, tmp_path
):
    _production(monkeypatch)
    import sandbox_runtime.activities as activities_mod
    import sandbox_runtime.repo_clone as repo_clone

    monkeypatch.setattr(activities_mod, "_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(activities_mod.git_checkpoint, "provision_checkpoint_repo", lambda *_args: None)
    monkeypatch.setattr(repo_clone, "mint_installation_token", lambda: None)
    monkeypatch.setattr(repo_clone, "clone_repo_into", lambda **_kwargs: False)

    with pytest.raises(RuntimeProfileViolation, match="workspace vazio"):
        asyncio.run(
            provision_sandbox(
                ProvisionSandboxInput(
                    work_item_id="wi-prod",
                    tenant_id="tenant-prod",
                    repo="acme/repo",
                )
            )
        )


def test_invalid_security_boolean_fails_closed(monkeypatch):
    _production(monkeypatch)
    monkeypatch.setenv(SANDBOX_INPROCESS_ENV_VAR, "talvez")
    with pytest.raises(RuntimeProfileViolation, match="booleano explícito"):
        validate_runtime_startup()
