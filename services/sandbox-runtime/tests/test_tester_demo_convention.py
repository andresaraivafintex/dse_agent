"""WSC-E3-T4b (b)+(c): the `demos/<work_item_id>/` convention in the
TesterToolset and authoring of the `@demo` fixture by the scripted Tester.

Proves (against real sandbox/git, same harness as the Phase 2 suite):
  - the Tester WRITES under `demos/<work_item_id>/` (an additional allowed
    path);
  - it stays BLOCKED elsewhere: production code, ANOTHER work item's `demos/`,
    and path traversal out of the prefix — all `ToolPermissionError`;
  - the `@demo` fixture (template committed in `sandbox_runtime.demo_fixture`)
    is materialized by the Tester and committed deterministically onto the task
    branch (this is what the WS-E pipeline executes — see
    tests/test_demo_playwright_in_sandbox.py for the real execution).
"""
from __future__ import annotations

import asyncio
import subprocess

import pytest

from sandbox_runtime.activities import (
    ProvisionSandboxInput,
    RunTesterTurnInput,
    TeardownSandboxInput,
    _paths_for,
    _run_tester_turn_impl,
    provision_sandbox,
    teardown_sandbox,
)
from sandbox_runtime.demo_fixture import demo_authoring_script, demo_fixture_files
from sandbox_runtime.toolsets import (
    TesterToolset,
    ToolInvocation,
    ToolPermissionError,
    demo_dir_for,
    is_demo_path,
)

WI = "wi-demo-conv-1"
OTHER_WI = "wi-other-task"


# ---------------------------------------------------------------------------
# Unit: demo path scope
# ---------------------------------------------------------------------------
def test_demo_dir_convention_matches_contract_default():
    """`RunDemoEvidenceInput.demo_dir` (the foundation contract) documents the
    derived default `demos/<work_item_id>/` — the convention here is the same."""
    assert demo_dir_for(WI) == f"demos/{WI}/"


def test_is_demo_path_scoped_to_own_work_item():
    assert is_demo_path(f"demos/{WI}/demo.spec.js", WI)
    assert is_demo_path(f"demos/{WI}/playwright.config.js", WI)
    assert not is_demo_path(f"demos/{OTHER_WI}/demo.spec.js", WI)
    assert not is_demo_path("demos/demo.spec.js", WI)
    assert not is_demo_path(f"demos/{WI}/../../src/app.py", WI)  # traversal
    assert not is_demo_path(f"demos/{WI}/x.js", "")  # no work item, no demo write


def _check(toolset: TesterToolset, path: str) -> None:
    toolset.check(ToolInvocation(tool="write_file", args={"path": path}))


def test_toolset_allows_demo_dir_and_blocks_everything_else():
    ts = TesterToolset(work_item_id=WI)
    # allowed: test paths (Phase 2) + demos/<this work item>/ (Phase 3)
    _check(ts, "tests/test_x.py")
    _check(ts, f"demos/{WI}/demo.spec.js")
    # blocked: production, another work item's demos, traversal
    with pytest.raises(ToolPermissionError):
        _check(ts, "src/handler.py")
    with pytest.raises(ToolPermissionError):
        _check(ts, f"demos/{OTHER_WI}/demo.spec.js")
    with pytest.raises(ToolPermissionError):
        _check(ts, f"demos/{WI}/../../src/handler.py")


def test_toolset_without_work_item_blocks_all_demo_writes():
    """Constructor without arguments (Phase 2 call sites): test paths keep
    working, and NO write under `demos/` is allowed — not even one that looks
    like a test path (`*.spec.js`): the evidence namespace is scoped per work
    item, and a session without a work item has no scope there at all."""
    ts = TesterToolset()
    _check(ts, "tests/test_x.py")
    _check(ts, "src/app.spec.js")  # generic Phase 2 rule, outside demos/
    with pytest.raises(ToolPermissionError):
        _check(ts, f"demos/{WI}/demo.spec.js")


# ---------------------------------------------------------------------------
# Integration: the Tester authors the @demo fixture and commits (real sandbox/git)
# ---------------------------------------------------------------------------
def test_tester_authors_demo_fixture_and_commits(work_item_id, state_dir):
    tenant = "tenant-t"
    asyncio.run(provision_sandbox(ProvisionSandboxInput(work_item_id=work_item_id, tenant_id=tenant)))
    try:
        result = asyncio.run(
            _run_tester_turn_impl(
                RunTesterTurnInput(work_item_id=work_item_id, tenant_id=tenant, instruction="author the @demo"),
                authoring_script=demo_authoring_script(work_item_id),
            )
        )
        expected_paths = sorted(demo_fixture_files(work_item_id))
        assert sorted(result.test_files) == expected_paths

        workspace, bare = _paths_for(work_item_id)
        for rel in expected_paths:
            content = (open(f"{workspace}/{rel}").read())
            assert content, f"{rel} empty in the workspace"
        spec = open(f"{workspace}/demos/{work_item_id}/demo.spec.js").read()
        assert "@demo" in spec, "the fixture spec must carry the @demo tag (WS-E --grep contract)"

        # real deterministic commit on the task branch
        log = subprocess.run(
            ["git", "log", "--oneline", f"dse/{work_item_id}"],
            cwd=bare, check=True, capture_output=True, text=True,
        ).stdout
        assert "tester(" in log
        files = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", f"dse/{work_item_id}"],
            cwd=bare, check=True, capture_output=True, text=True,
        ).stdout.splitlines()
        for rel in expected_paths:
            assert rel in files
    finally:
        asyncio.run(teardown_sandbox(TeardownSandboxInput(work_item_id=work_item_id, tenant_id=tenant)))


def test_tester_still_blocked_outside_demo_and_test_paths(work_item_id, state_dir):
    tenant = "tenant-t"
    asyncio.run(provision_sandbox(ProvisionSandboxInput(work_item_id=work_item_id, tenant_id=tenant)))
    try:
        # ANOTHER work item's demos/ — blocked even with the convention active.
        with pytest.raises(ToolPermissionError):
            asyncio.run(
                _run_tester_turn_impl(
                    RunTesterTurnInput(work_item_id=work_item_id, tenant_id=tenant, instruction="cross-demo"),
                    authoring_script=[
                        {"tool": "write_file", "path": f"demos/{OTHER_WI}/demo.spec.js", "content": "// x"},
                    ],
                )
            )
        # production — still blocked (Phase 2 regression).
        with pytest.raises(ToolPermissionError):
            asyncio.run(
                _run_tester_turn_impl(
                    RunTesterTurnInput(work_item_id=work_item_id, tenant_id=tenant, instruction="production"),
                    authoring_script=[
                        {"tool": "write_file", "path": "src/app.py", "content": "x = 1"},
                    ],
                )
            )
    finally:
        asyncio.run(teardown_sandbox(TeardownSandboxInput(work_item_id=work_item_id, tenant_id=tenant)))
