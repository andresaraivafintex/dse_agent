from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from dse_validation.sandbox_exec import LocalFakeSandbox


def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo_dir), capture_output=True, text=True, check=True
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Repo git real (não mockado) com um commit inicial em `main` — usado
    pelos testes de plan_compliance (git diff --numstat real) e de quality
    checks (lint/typecheck/test/build reais via LocalFakeSandbox)."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _git(repo_dir, "init", "-q", "-b", "main")
    _git(repo_dir, "config", "user.email", "coder@dse.local")
    _git(repo_dir, "config", "user.name", "DSE Coder")
    (repo_dir / "app.py").write_text("def add(a, b):\n    return a + b\n")
    (repo_dir / "test_app.py").write_text(
        "from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-q", "-m", "initial commit")
    return repo_dir


@pytest.fixture
def feature_branch(git_repo: Path):
    """Cria e faz checkout de um branch de feature a partir de `main` —
    devolve uma função para commitar mudanças adicionais no branch."""
    _git(git_repo, "checkout", "-q", "-b", "feature/test")

    def commit_change(relpath: str, content: str, message: str = "feature change") -> None:
        path = git_repo / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        _git(git_repo, "add", "-A")
        _git(git_repo, "commit", "-q", "-m", message)

    return commit_change


@pytest.fixture
def sandbox(git_repo: Path) -> LocalFakeSandbox:
    return LocalFakeSandbox(git_repo)


@pytest.fixture
def tenant_id() -> str:
    return f"acme-test-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def work_item_id() -> str:
    return f"wi_test_{uuid.uuid4().hex[:8]}"
