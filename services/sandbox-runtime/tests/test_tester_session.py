"""WSC-E3-T4: Tester session.

Proves (against real sandbox/git):
  - the `run_tester_turn` Activity is a Temporal Activity named after the
    contract;
  - edits are allowed ONLY under test paths — writing to production code FAILS
    (`ToolPermissionError`);
  - the written tests actually EXECUTE (real pytest in the workspace), they are
    not merely generated;
  - the test files are committed/pushed by deterministic code onto the task
    branch.
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
                RunTesterTurnInput(work_item_id=work_item_id, tenant_id=tenant, instruction="cover the handler"),
                authoring_script=[
                    {"tool": "write_file", "path": "tests/test_generated.py", "content": _PASSING_TEST},
                    {"tool": "run_tests", "paths": ["tests/test_generated.py"]},
                ],
            )
        )
        assert result.test_files == ["tests/test_generated.py"]
        assert result.tests_ran is True
        assert result.tests_passed is True, "the test that was written should really run and really pass"
        assert result.returncode == 0

        # real commit in the bare repo on the task branch
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
                    RunTesterTurnInput(work_item_id=work_item_id, tenant_id=tenant, instruction="tries to edit code"),
                    authoring_script=[
                        {"tool": "write_file", "path": "src/handler.py", "content": "def handler(): return 'hacked'"},
                    ],
                )
            )
    finally:
        asyncio.run(teardown_sandbox(TeardownSandboxInput(work_item_id=work_item_id, tenant_id=tenant)))


def test_tester_reports_real_failure_when_written_test_fails(work_item_id, state_dir):
    """A test that genuinely fails is reported as a failure (not 'generated
    ok') — proof that execution is real, not simulated."""
    tenant = "tenant-t"
    asyncio.run(provision_sandbox(ProvisionSandboxInput(work_item_id=work_item_id, tenant_id=tenant)))
    try:
        result = asyncio.run(
            _run_tester_turn_impl(
                RunTesterTurnInput(work_item_id=work_item_id, tenant_id=tenant, instruction="a test that fails"),
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
