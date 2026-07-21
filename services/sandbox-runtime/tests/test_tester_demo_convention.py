"""WSC-E3-T4b (b)+(c): convenção `demos/<work_item_id>/` no TesterToolset e
autoria do fixture `@demo` pelo Tester roteirizado.

Prova (contra sandbox/git reais, mesmo harness da suíte da Fase 2):
  - o Tester ESCREVE em `demos/<work_item_id>/` (path permitido adicional);
  - continua BLOQUEADO fora: código de produção, `demos/` de OUTRO work item,
    e path traversal para fora do prefixo — tudo `ToolPermissionError`;
  - o fixture `@demo` (template commitado em `sandbox_runtime.demo_fixture`)
    é materializado pelo Tester e commitado deterministicamente no branch da
    tarefa (é o que o pipeline do WS-E executa — ver
    tests/test_demo_playwright_in_sandbox.py para a execução real).
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
OTHER_WI = "wi-outra-tarefa"


# ---------------------------------------------------------------------------
# Unidade: escopo do path de demo
# ---------------------------------------------------------------------------
def test_demo_dir_convention_matches_contract_default():
    """`RunDemoEvidenceInput.demo_dir` (contrato da fundação) documenta o
    default derivado `demos/<work_item_id>/` — a convenção daqui é a mesma."""
    assert demo_dir_for(WI) == f"demos/{WI}/"


def test_is_demo_path_scoped_to_own_work_item():
    assert is_demo_path(f"demos/{WI}/demo.spec.js", WI)
    assert is_demo_path(f"demos/{WI}/playwright.config.js", WI)
    assert not is_demo_path(f"demos/{OTHER_WI}/demo.spec.js", WI)
    assert not is_demo_path("demos/demo.spec.js", WI)
    assert not is_demo_path(f"demos/{WI}/../../src/app.py", WI)  # traversal
    assert not is_demo_path(f"demos/{WI}/x.js", "")  # sem work item, sem demo write


def _check(toolset: TesterToolset, path: str) -> None:
    toolset.check(ToolInvocation(tool="write_file", args={"path": path}))


def test_toolset_allows_demo_dir_and_blocks_everything_else():
    ts = TesterToolset(work_item_id=WI)
    # permitido: test paths (Fase 2) + demos/<este work item>/ (Fase 3)
    _check(ts, "tests/test_x.py")
    _check(ts, f"demos/{WI}/demo.spec.js")
    # bloqueado: produção, demos de outro work item, traversal
    with pytest.raises(ToolPermissionError):
        _check(ts, "src/handler.py")
    with pytest.raises(ToolPermissionError):
        _check(ts, f"demos/{OTHER_WI}/demo.spec.js")
    with pytest.raises(ToolPermissionError):
        _check(ts, f"demos/{WI}/../../src/handler.py")


def test_toolset_without_work_item_blocks_all_demo_writes():
    """Construtor sem argumento (call sites da Fase 2): test paths continuam
    funcionando, e NENHUM write em `demos/` é permitido — nem mesmo um que
    pareça test path (`*.spec.js`): o namespace de evidência é escopado por
    work item, e sessão sem work item não tem escopo nenhum lá."""
    ts = TesterToolset()
    _check(ts, "tests/test_x.py")
    _check(ts, "src/app.spec.js")  # regra genérica da Fase 2 fora de demos/
    with pytest.raises(ToolPermissionError):
        _check(ts, f"demos/{WI}/demo.spec.js")


# ---------------------------------------------------------------------------
# Integração: Tester autora o fixture @demo e commita (sandbox/git reais)
# ---------------------------------------------------------------------------
def test_tester_authors_demo_fixture_and_commits(work_item_id, state_dir):
    tenant = "tenant-t"
    asyncio.run(provision_sandbox(ProvisionSandboxInput(work_item_id=work_item_id, tenant_id=tenant)))
    try:
        result = asyncio.run(
            _run_tester_turn_impl(
                RunTesterTurnInput(work_item_id=work_item_id, tenant_id=tenant, instruction="autora o @demo"),
                authoring_script=demo_authoring_script(work_item_id),
            )
        )
        expected_paths = sorted(demo_fixture_files(work_item_id))
        assert sorted(result.test_files) == expected_paths

        workspace, bare = _paths_for(work_item_id)
        for rel in expected_paths:
            content = (open(f"{workspace}/{rel}").read())
            assert content, f"{rel} vazio no workspace"
        spec = open(f"{workspace}/demos/{work_item_id}/demo.spec.js").read()
        assert "@demo" in spec, "spec do fixture precisa carregar a tag @demo (contrato --grep do WS-E)"

        # commit determinístico real no branch da tarefa
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
        # demos/ de OUTRO work item — bloqueado mesmo com a convenção ativa.
        with pytest.raises(ToolPermissionError):
            asyncio.run(
                _run_tester_turn_impl(
                    RunTesterTurnInput(work_item_id=work_item_id, tenant_id=tenant, instruction="cross-demo"),
                    authoring_script=[
                        {"tool": "write_file", "path": f"demos/{OTHER_WI}/demo.spec.js", "content": "// x"},
                    ],
                )
            )
        # produção — continua bloqueado (regressão da Fase 2).
        with pytest.raises(ToolPermissionError):
            asyncio.run(
                _run_tester_turn_impl(
                    RunTesterTurnInput(work_item_id=work_item_id, tenant_id=tenant, instruction="produção"),
                    authoring_script=[
                        {"tool": "write_file", "path": "src/app.py", "content": "x = 1"},
                    ],
                )
            )
    finally:
        asyncio.run(teardown_sandbox(TeardownSandboxInput(work_item_id=work_item_id, tenant_id=tenant)))
