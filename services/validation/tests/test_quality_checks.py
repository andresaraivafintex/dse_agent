"""WSE-E1-T1 — prova que lint/typecheck/test/build rodam de verdade (subprocess
real de ruff/mypy/pytest/compileall via LocalFakeSandbox) e que o parsing é
estruturado (conta issues/erros, não só olha o exit code)."""
from __future__ import annotations

from dse_validation.config import L1Config
from dse_validation.l1.quality_checks import build_check, lint_check, typecheck_check
from dse_validation.l1.quality_checks import test_check as run_test_check


def test_lint_check_passes_on_clean_code(sandbox):
    cfg = L1Config()
    finding = lint_check(sandbox, cfg)
    assert finding.check == "lint"
    assert finding.passed is True


def test_lint_check_fails_and_reports_issue_count(sandbox, git_repo):
    (git_repo / "bad.py").write_text("import os\nx=1\n")  # unused import + missing spaces (ruff issues)
    cfg = L1Config()
    finding = lint_check(sandbox, cfg)
    assert finding.check == "lint"
    assert finding.passed is False
    assert "issue" in finding.detail


def test_typecheck_check_runs_real_mypy(sandbox):
    cfg = L1Config()
    finding = typecheck_check(sandbox, cfg)
    assert finding.check == "typecheck"
    # repo fixture não tem erro de tipo declarado -> deve passar
    assert finding.passed is True


def test_typecheck_check_fails_on_real_type_error(sandbox, git_repo):
    (git_repo / "typed.py").write_text("def f(x: int) -> int:\n    return x + 'a'\n")
    cfg = L1Config()
    finding = typecheck_check(sandbox, cfg)
    assert finding.passed is False
    assert "erro" in finding.detail.lower()


def test_test_check_runs_real_pytest_and_passes(sandbox):
    cfg = L1Config()
    finding = run_test_check(sandbox, cfg)
    assert finding.check == "test"
    assert finding.passed is True
    assert "1 passed" in finding.detail


def test_test_check_fails_on_real_failing_test(sandbox, git_repo):
    (git_repo / "test_broken.py").write_text("def test_broken():\n    assert 1 == 2\n")
    cfg = L1Config()
    finding = run_test_check(sandbox, cfg)
    assert finding.passed is False
    assert "failed" in finding.detail


def test_build_check_runs_real_compileall(sandbox):
    cfg = L1Config()
    finding = build_check(sandbox, cfg)
    assert finding.check == "build"
    assert finding.passed is True


def test_build_check_fails_on_syntax_error(sandbox, git_repo):
    (git_repo / "broken_syntax.py").write_text("def f(:\n    pass\n")
    cfg = L1Config()
    finding = build_check(sandbox, cfg)
    assert finding.passed is False


def test_unknown_command_is_reported_not_silently_skipped(sandbox, monkeypatch):
    monkeypatch.setenv("DSE_L1_LINT_CMD", "this-tool-does-not-exist")
    cfg = L1Config()
    finding = lint_check(sandbox, cfg)
    assert finding.passed is False
    assert "não encontrado" in finding.detail
