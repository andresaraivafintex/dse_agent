"""WSE-E1-T1 — proves that lint/typecheck/test/build actually run (real
ruff/mypy/pytest/compileall subprocesses via LocalFakeSandbox) and that parsing is
structured (counts issues/errors, does not just look at the exit code)."""
from __future__ import annotations

from dse_contracts import GateStatus

from dse_validation.config import L1Config
from dse_validation.l1.quality_checks import build_check, lint_check, typecheck_check
from dse_validation.l1.quality_checks import test_check as run_test_check
from dse_validation.sandbox_exec import ExecResult


def test_lint_check_passes_on_clean_code(sandbox):
    cfg = L1Config.for_test_repo()
    finding = lint_check(sandbox, cfg)
    assert finding.check == "lint"
    assert finding.passed is True


def test_lint_check_fails_and_reports_issue_count(sandbox, git_repo):
    (git_repo / "bad.py").write_text("import os\nx=1\n")  # unused import + missing spaces (ruff issues)
    cfg = L1Config.for_test_repo()
    finding = lint_check(sandbox, cfg)
    assert finding.check == "lint"
    assert finding.passed is False
    assert "issue" in finding.detail


def test_typecheck_check_runs_real_mypy(sandbox):
    cfg = L1Config.for_test_repo()
    finding = typecheck_check(sandbox, cfg)
    assert finding.check == "typecheck"
    # the fixture repo has no declared type error -> must pass
    assert finding.passed is True


def test_typecheck_check_fails_on_real_type_error(sandbox, git_repo):
    (git_repo / "typed.py").write_text("def f(x: int) -> int:\n    return x + 'a'\n")
    cfg = L1Config.for_test_repo()
    finding = typecheck_check(sandbox, cfg)
    assert finding.passed is False
    assert "error" in finding.detail.lower()


def test_test_check_runs_real_pytest_and_passes(sandbox):
    cfg = L1Config.for_test_repo()
    finding = run_test_check(sandbox, cfg)
    assert finding.check == "test"
    assert finding.passed is True
    assert "1 passed" in finding.detail


def test_test_check_fails_on_real_failing_test(sandbox, git_repo):
    (git_repo / "test_broken.py").write_text("def test_broken():\n    assert 1 == 2\n")
    cfg = L1Config.for_test_repo()
    finding = run_test_check(sandbox, cfg)
    assert finding.passed is False
    assert "failed" in finding.detail


def test_build_check_runs_real_compileall(sandbox):
    cfg = L1Config.for_test_repo()
    finding = build_check(sandbox, cfg)
    assert finding.check == "build"
    assert finding.passed is True


def test_build_check_fails_on_syntax_error(sandbox, git_repo):
    (git_repo / "broken_syntax.py").write_text("def f(:\n    pass\n")
    cfg = L1Config.for_test_repo()
    finding = build_check(sandbox, cfg)
    assert finding.passed is False


def test_unknown_command_is_reported_not_silently_skipped(sandbox):
    cfg = L1Config(lint_cmd=["this-tool-does-not-exist"])
    finding = lint_check(sandbox, cfg)
    assert finding.passed is False
    assert finding.status == GateStatus.ERROR
    assert "not found" in finding.detail


def test_empty_commands_are_not_configured_never_green(sandbox):
    cfg = L1Config()
    findings = [
        lint_check(sandbox, cfg),
        typecheck_check(sandbox, cfg),
        run_test_check(sandbox, cfg),
        build_check(sandbox, cfg),
    ]
    assert all(f.passed is False for f in findings)
    assert all(f.status == GateStatus.NOT_CONFIGURED for f in findings)


class _RecordingSandbox:
    """Captures the timeout each check hands to the executor. Nothing else makes
    the number observable — and a per-stage budget that never reaches the
    executor is a manifest field that validates, escalates, and does nothing."""

    def __init__(self) -> None:
        self.timeouts: list[int] = []

    def run(self, argv, cwd=None, timeout: int = 300) -> ExecResult:
        self.timeouts.append(timeout)
        return ExecResult(argv=argv, returncode=0, stdout="", stderr="")


def test_the_manifests_per_stage_timeout_is_the_one_that_runs():
    """`timeouts` is validated against the activity's budget and can ERROR a work
    item; if the value then never reaches the executor, the guard is policing a
    number nothing uses while the stage runs on the scalar."""
    cfg = L1Config(
        lint_cmd=["ruff"], typecheck_cmd=["mypy"], test_cmd=["pytest"], build_cmd=["make"],
        timeout_seconds=300,
        timeouts={"lint": 30, "test": 700},
    )
    box = _RecordingSandbox()

    lint_check(box, cfg)
    typecheck_check(box, cfg)
    run_test_check(box, cfg)
    build_check(box, cfg)

    # declared -> declared; not declared -> the scalar, exactly as before.
    assert box.timeouts == [30, 300, 700, 300]


def test_a_timed_out_stage_names_the_budget_that_actually_ran():
    """The message is what a human reads when L1 goes ERROR. Printing the scalar
    while the stage ran on `timeouts.test` sends them to the wrong knob."""

    class _Slow(_RecordingSandbox):
        def run(self, argv, cwd=None, timeout: int = 300) -> ExecResult:
            super().run(argv, cwd=cwd, timeout=timeout)
            return ExecResult(argv=argv, returncode=-1, stdout="", stderr="", timed_out=True)

    cfg = L1Config(test_cmd=["pytest"], timeout_seconds=300, timeouts={"test": 700})
    finding = run_test_check(_Slow(), cfg)

    assert finding.status == GateStatus.ERROR
    assert "700s" in finding.detail


# ---------------------------------------------------------------------------
# A failing gate must never report the opposite of its own verdict.
#
# Both cases below are transcripts of a REAL L1 run on the Angular testbed
# (wi_pr21, 2026-08-05): lint FAILED reading "no lint issues" and typecheck
# FAILED reading "no type errors", because the parsers only knew ruff's and
# mypy's output shapes. That reason is what the ledger publishes.
# ---------------------------------------------------------------------------
class _CannedSandbox:
    """Replays one recorded ExecResult, whatever it is asked to run."""

    def __init__(self, result: ExecResult):
        self._result = result

    def run(self, argv, timeout=None):  # noqa: ARG002 - signature parity
        return self._result


def _canned(stdout: str, returncode: int) -> _CannedSandbox:
    return _CannedSandbox(
        ExecResult(argv=["x"], returncode=returncode, stdout=stdout, stderr="")
    )


_ESLINT_OUTPUT = """
> bmo-fee-estimator-fe@0.0.0 lint
> ng lint

/src/app/app.component.ts
  12:7  error  'unused' is assigned a value but never used  @typescript-eslint/no-unused-vars

1 problem (1 error, 0 warnings)
"""


def test_a_failed_lint_never_claims_there_were_no_issues():
    cfg = L1Config(lint_cmd=["npm", "run", "lint"])
    finding = lint_check(_canned(_ESLINT_OUTPUT, 1), cfg)
    assert finding.passed is False
    assert "no lint issues" not in finding.summary
    assert "exit=1" in finding.summary


_TSC_OUTPUT = (
    "src/app/shared/services/pdf-collect-data.service.spec.ts(690,48): "
    "error TS2345: Argument of type '() => string' is not assignable\n"
    "src/app/shared/services/pdf-data.service.spec.ts(57,5): "
    "error TS2739: Type '{}' is missing the following properties\n"
)


def test_tsc_diagnostics_are_counted_not_just_mypy_ones():
    cfg = L1Config(typecheck_cmd=["npx", "tsc", "--noEmit"])
    finding = typecheck_check(_canned(_TSC_OUTPUT, 2), cfg)
    assert finding.passed is False
    assert finding.summary == "2 type error(s)"


def test_a_failed_typecheck_never_claims_there_were_no_errors():
    """Unrecognised output must degrade to an honest line, not to a denial."""
    cfg = L1Config(typecheck_cmd=["npx", "tsc", "--noEmit"])
    finding = typecheck_check(_canned("something nobody parses\n", 2), cfg)
    assert finding.passed is False
    assert "no type errors" not in finding.summary
    assert "exit=2" in finding.summary
