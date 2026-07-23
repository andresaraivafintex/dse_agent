"""WSC-E3-T4: sessão Tester.

Prova (contra sandbox/git reais):
  - a Activity `run_tester_turn` é uma Activity Temporal com o nome do contrato;
  - edits são permitidos SÓ em test paths — escrever em código de produção
    FALHA (`ToolPermissionError`);
  - os testes escritos EXECUTAM de verdade (pytest real no workspace), não são
    só gerados;
  - os test files são commitados/pushados por código determinístico ao branch
    da tarefa.
"""
from __future__ import annotations

import asyncio
import subprocess

import pytest
from temporalio.testing import ActivityEnvironment

from dse_contracts import ACTIVITY_RUN_TESTER_TURN
from sandbox_runtime.activities import (
    ProvisionSandboxInput,
    RunTesterTurnInput,
    TeardownSandboxInput,
    _paths_for,
    _run_tester_turn_impl,
    provision_sandbox,
    run_tester_turn,
    teardown_sandbox,
)
from sandbox_runtime.toolsets import ToolPermissionError

_PASSING_TEST = "def test_generated_passes():\n    assert 1 + 1 == 2\n"
_FAILING_TEST = "def test_generated_fails():\n    assert 1 + 1 == 3\n"


def test_activity_name_matches_contract():
    assert run_tester_turn.__temporal_activity_definition.name == ACTIVITY_RUN_TESTER_TURN


def test_tester_writes_test_path_runs_pytest_and_commits(work_item_id, state_dir):
    tenant = "tenant-t"
    asyncio.run(provision_sandbox(ProvisionSandboxInput(work_item_id=work_item_id, tenant_id=tenant)))
    try:
        result = asyncio.run(
            _run_tester_turn_impl(
                RunTesterTurnInput(work_item_id=work_item_id, tenant_id=tenant, instruction="cobre o handler"),
                authoring_script=[
                    {"tool": "write_file", "path": "tests/test_generated.py", "content": _PASSING_TEST},
                    {"tool": "run_tests", "paths": ["tests/test_generated.py"]},
                ],
            )
        )
        assert result.test_files == ["tests/test_generated.py"]
        assert result.tests_ran is True
        assert result.tests_passed is True, "o teste escrito deveria rodar e passar de verdade"
        assert result.returncode == 0

        # commit real no bare repo do branch da tarefa
        _workspace, bare = _paths_for(work_item_id)
        log = subprocess.run(
            ["git", "log", "--oneline", f"dse/{work_item_id}"], cwd=bare, check=True, capture_output=True, text=True
        ).stdout
        assert "tester(" in log
    finally:
        asyncio.run(teardown_sandbox(TeardownSandboxInput(work_item_id=work_item_id, tenant_id=tenant)))


def test_tester_write_to_production_path_fails(work_item_id, state_dir):
    tenant = "tenant-t"
    asyncio.run(provision_sandbox(ProvisionSandboxInput(work_item_id=work_item_id, tenant_id=tenant)))
    try:
        with pytest.raises(ToolPermissionError):
            asyncio.run(
                _run_tester_turn_impl(
                    RunTesterTurnInput(work_item_id=work_item_id, tenant_id=tenant, instruction="tenta editar código"),
                    authoring_script=[
                        {"tool": "write_file", "path": "src/handler.py", "content": "def handler(): return 'hacked'"},
                    ],
                )
            )
    finally:
        asyncio.run(teardown_sandbox(TeardownSandboxInput(work_item_id=work_item_id, tenant_id=tenant)))


def test_tester_reports_real_failure_when_written_test_fails(work_item_id, state_dir):
    """Um teste que falha de verdade é reportado como falha (não 'gerado ok') —
    prova que a execução é real, não simulada."""
    tenant = "tenant-t"
    asyncio.run(provision_sandbox(ProvisionSandboxInput(work_item_id=work_item_id, tenant_id=tenant)))
    try:
        result = asyncio.run(
            _run_tester_turn_impl(
                RunTesterTurnInput(work_item_id=work_item_id, tenant_id=tenant, instruction="teste que falha"),
                authoring_script=[
                    {"tool": "write_file", "path": "tests/test_fail.py", "content": _FAILING_TEST},
                    {"tool": "run_tests", "paths": ["tests/test_fail.py"]},
                ],
            )
        )
        assert result.tests_ran is True
        assert result.tests_passed is False
        assert result.returncode != 0
    finally:
        asyncio.run(teardown_sandbox(TeardownSandboxInput(work_item_id=work_item_id, tenant_id=tenant)))


def test_tester_runs_through_real_temporal_activity_environment(work_item_id, state_dir):
    tenant = "tenant-t"
    asyncio.run(provision_sandbox(ProvisionSandboxInput(work_item_id=work_item_id, tenant_id=tenant)))
    env = ActivityEnvironment()
    try:
        result = asyncio.run(
            env.run(
                run_tester_turn,
                RunTesterTurnInput(work_item_id=work_item_id, tenant_id=tenant, instruction="smoke"),
            )
        )
        assert result.sandbox_id == work_item_id
    finally:
        asyncio.run(teardown_sandbox(TeardownSandboxInput(work_item_id=work_item_id, tenant_id=tenant)))
