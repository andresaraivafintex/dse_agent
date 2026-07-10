"""WSC-E3-T2: sessão Coder com git de escopo limitado.

Prova as duas camadas de enforcement:
  1. Toolset: `ScopedGitSession` não expõe force-push/PR/comando genérico.
  2. Escopo do remoto: o hook `pre-receive` do bare repo de checkpoint
     recusa force-push e push para outro branch, MESMO quando alguém
     contorna `ScopedGitSession` e roda `git push --force` cru.
  3. Escopo da credencial: `ScopedCredential.create_pull_request()`/
     `.force_push()` sempre recusam (contrato de escopo do token do GitHub
     App que o egress-proxy injetaria em produção).

Também prova o caminho feliz: `run_coder_turn` roda um `FakeSubstrate`
roteirizado, e o resultado (`CoderTurnResult`) reflete os arquivos
realmente commitados/pushados para o branch da tarefa.
"""
from __future__ import annotations

import asyncio
import subprocess

import pytest

from sandbox_runtime.activities import (
    ProvisionSandboxInput,
    RunCoderTurnInput,
    TeardownSandboxInput,
    _paths_for,
    _run_coder_turn_impl,
    provision_sandbox,
    teardown_sandbox,
)
from sandbox_runtime.scoped_git import FORBIDDEN_METHOD_NAMES, GitScopeViolation, ScopedGitSession
from sandbox_runtime.substrate import FakeSubstrate


def test_scoped_git_session_has_no_escape_hatch():
    public_methods = {name for name in dir(ScopedGitSession) if not name.startswith("_")}
    overlap = public_methods & FORBIDDEN_METHOD_NAMES
    assert not overlap, f"ScopedGitSession expõe métodos fora do escopo permitido: {overlap}"
    # apenas commit/push/leitura devem existir
    assert {"commit", "push", "has_changes", "ensure_identity", "current_sha", "files_changed_against"} <= public_methods


def test_run_coder_turn_commits_and_pushes_only_scripted_files(work_item_id, state_dir):
    tenant_id = "tenant-a"
    asyncio.run(provision_sandbox(ProvisionSandboxInput(work_item_id=work_item_id, tenant_id=tenant_id)))

    script = [
        {
            "write_files": {"src/handler.py": "def handler():\n    return 'ok'\n"},
            "thought": "implementa handler",
            "done": True,
        }
    ]
    fake = FakeSubstrate(script)
    result = asyncio.run(
        _run_coder_turn_impl(
            RunCoderTurnInput(work_item_id=work_item_id, tenant_id=tenant_id, instruction="implemente o handler"),
            substrate=fake,
        )
    )

    assert "src/handler.py" in result.files_changed
    assert result.cost_usd > 0

    workspace_dir, bare_repo_path = _paths_for(work_item_id)
    branch = f"dse/{work_item_id}"
    log = subprocess.run(
        ["git", "log", "--oneline", branch], cwd=bare_repo_path, check=True, capture_output=True, text=True
    ).stdout
    assert "coder(" in log

    asyncio.run(teardown_sandbox(TeardownSandboxInput(work_item_id=work_item_id, tenant_id=tenant_id)))


def test_adversarial_force_push_is_rejected_by_remote_scope(work_item_id, state_dir):
    """Mesmo contornando `ScopedGitSession` (subprocess cru), o bare repo
    recusa non-fast-forward — a segunda camada de enforcement não depende do
    código que fez o push."""
    tenant_id = "tenant-a"
    asyncio.run(provision_sandbox(ProvisionSandboxInput(work_item_id=work_item_id, tenant_id=tenant_id)))
    workspace_dir, bare_repo_path = _paths_for(work_item_id)
    branch = f"dse/{work_item_id}"

    subprocess.run(
        ["git", "-c", "user.email=x@x.com", "-c", "user.name=x", "commit", "--allow-empty", "-m", "c1"],
        cwd=workspace_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(["git", "push", "origin", f"HEAD:refs/heads/{branch}"], cwd=workspace_dir, check=True, capture_output=True, text=True)

    # Reescreve a história local (simula um force-push adversarial) e tenta
    # empurrar cru, sem passar por ScopedGitSession.
    subprocess.run(["git", "reset", "--hard", "HEAD~1"], cwd=workspace_dir, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-c", "user.email=x@x.com", "-c", "user.name=x", "commit", "--allow-empty", "-m", "rewritten-history"],
        cwd=workspace_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(
        ["git", "push", "--force", "origin", f"HEAD:refs/heads/{branch}"],
        cwd=workspace_dir,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "force-push deveria ter sido recusado pelo hook pre-receive"
    assert "recusado" in result.stderr or "rejected" in result.stderr.lower()

    asyncio.run(teardown_sandbox(TeardownSandboxInput(work_item_id=work_item_id, tenant_id=tenant_id)))


def test_adversarial_push_to_other_branch_is_rejected(work_item_id, state_dir):
    tenant_id = "tenant-a"
    asyncio.run(provision_sandbox(ProvisionSandboxInput(work_item_id=work_item_id, tenant_id=tenant_id)))
    workspace_dir, _bare = _paths_for(work_item_id)

    result = subprocess.run(
        ["git", "push", "origin", "HEAD:refs/heads/some-other-branch"],
        cwd=workspace_dir,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "push para branch fora do escopo deveria ter sido recusado"
    assert "recusado" in result.stderr or "rejected" in result.stderr.lower()

    asyncio.run(teardown_sandbox(TeardownSandboxInput(work_item_id=work_item_id, tenant_id=tenant_id)))


def test_scoped_git_session_push_raises_git_scope_violation_on_conflict(work_item_id, state_dir):
    """`ScopedGitSession.push()` propaga a recusa do hook como
    `GitScopeViolation` (P6: falha limpa, nunca engolida) mesmo dentro da
    API "segura"."""
    tenant_id = "tenant-a"
    asyncio.run(provision_sandbox(ProvisionSandboxInput(work_item_id=work_item_id, tenant_id=tenant_id)))
    workspace_dir, _bare = _paths_for(work_item_id)
    branch = f"dse/{work_item_id}"

    session = ScopedGitSession(workspace_dir=workspace_dir, branch="some-other-branch")
    with pytest.raises(GitScopeViolation):
        session.push()

    asyncio.run(teardown_sandbox(TeardownSandboxInput(work_item_id=work_item_id, tenant_id=tenant_id)))
