from __future__ import annotations

import json
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
    """Real (unmocked) git repo with an initial commit on `main` — used by the
    plan_compliance tests (real git diff --numstat) and by the quality-check
    tests (real lint/typecheck/test/build via LocalFakeSandbox)."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _git(repo_dir, "init", "-q", "-b", "main")
    _git(repo_dir, "config", "user.email", "coder@dse.local")
    _git(repo_dir, "config", "user.name", "DSE Coder")
    (repo_dir / "app.py").write_text("def add(a, b):\n    return a + b\n")
    (repo_dir / "test_app.py").write_text(
        "from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    manifest_dir = repo_dir / ".dse"
    manifest_dir.mkdir()
    (manifest_dir / "validation.json").write_text(
        json.dumps(
            {
                "version": 1,
                "commands": {
                    "lint": ["ruff", "check", "."],
                    "typecheck": ["mypy", "."],
                    "test": ["pytest", "-q"],
                    "build": ["python", "-m", "compileall", "-q", "."],
                },
                "timeout_seconds": 300,
                "sast_severity_gate": "MEDIUM",
            }
        )
        + "\n"
    )
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-q", "-m", "initial commit")
    return repo_dir


@pytest.fixture
def feature_branch(git_repo: Path):
    """Creates and checks out a feature branch off `main` — returns a function
    to commit additional changes on that branch."""
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
def git_sha(git_repo: Path):
    """Resolves SHAs at call time, after any commits that may have happened."""

    def resolve(ref: str = "HEAD") -> str:
        return _git(git_repo, "rev-parse", ref).stdout.strip()

    return resolve


@pytest.fixture
def tenant_id() -> str:
    return f"acme-test-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def work_item_id() -> str:
    return f"wi_test_{uuid.uuid4().hex[:8]}"
