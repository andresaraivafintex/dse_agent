"""WSE-E1-T1 — deterministic in-sandbox gates: lint, typecheck, test, build.

Each check runs the configured command (dse_validation.config.L1Config) through a
`SandboxExecutor` (inside the real sandbox once WS-C ships the runtime; through
`LocalFakeSandbox` in test/dev) and parses the result in a STRUCTURED way —
never just the raw exit code — because the exit code alone says neither "how
many problems" nor gives readable evidence for `L1Finding.detail`
(P8: evidence over assertion).
"""
from __future__ import annotations

import re

from dse_contracts import GateStatus, L1Finding

from dse_validation.config import L1Config
from dse_validation.sandbox_exec import ExecResult, SandboxExecutor

_MAX_DETAIL_LINES = 40


def _tail(text: str, max_lines: int = _MAX_DETAIL_LINES) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    omitted = len(lines) - max_lines
    return "\n".join(lines[-max_lines:]) + f"\n... ({omitted} earlier line(s) omitted)"


def _run(executor: SandboxExecutor, argv: list[str], timeout: int) -> ExecResult:
    return executor.run(argv, timeout=timeout)


def _not_configured(check: str, cfg: L1Config) -> L1Finding:
    # `cfg.source` stays out of `summary`: it interpolates the manifest path and
    # base sha, and `summary` is the one field that reaches the append-only
    # ledger. The operator gets the same fact without them.
    return L1Finding(
        check=check,
        passed=False,
        status=GateStatus.NOT_CONFIGURED,
        detail=f"no {check} command in the trusted manifest {cfg.source}",
        summary=f"no {check} command in the trusted manifest",
    )


def lint_check(executor: SandboxExecutor, cfg: L1Config) -> L1Finding:
    if not cfg.lint_cmd:
        return _not_configured("lint", cfg)
    # `timeout_for`, not `timeout_seconds`: the manifest's per-stage `timeouts`
    # block is the clock that was validated against the activity's budget, and
    # the number in the message below has to be the one that actually ran.
    timeout = cfg.timeout_for("lint")
    result = _run(executor, cfg.lint_cmd, timeout)
    # ruff/flake8: 1 line per issue, formatted as "path:line:col: CODE msg".
    issue_lines = [ln for ln in result.stdout.splitlines() if re.match(r"^\S+:\d+:\d+:\s", ln)]
    passed = result.ok and len(issue_lines) == 0
    if result.timed_out:
        detail = f"timed out after {timeout}s running {' '.join(cfg.lint_cmd)}"
        summary = f"timed out after {timeout}s"
        status = GateStatus.ERROR
    elif result.returncode == 127:
        detail = f"lint command not found: {' '.join(cfg.lint_cmd)} ({result.stderr.strip()})"
        # Neither the argv nor the stderr belongs in `summary`. Both come from
        # the customer's own manifest: a repo can declare `lint: ["./ci/lint.sh"]`
        # and have that script dump the sandbox's environment to stderr before
        # exiting 127. That is the leak this field exists to close.
        summary = "lint command not found (exit 127)"
        passed = False
        status = GateStatus.ERROR
    else:
        n = len(issue_lines)
        summary = f"{n} lint issue(s)" if n else "no lint issues"
        detail = summary + "\n" + _tail(result.stdout or result.stderr)
        status = GateStatus.PASS if passed else GateStatus.FAIL
    return L1Finding(
        check="lint", passed=passed, status=status, detail=detail, summary=summary
    )


def typecheck_check(executor: SandboxExecutor, cfg: L1Config) -> L1Finding:
    if not cfg.typecheck_cmd:
        return _not_configured("typecheck", cfg)
    timeout = cfg.timeout_for("typecheck")
    result = _run(executor, cfg.typecheck_cmd, timeout)
    error_lines = [ln for ln in result.stdout.splitlines() if ": error:" in ln]
    if result.returncode == 127:
        return L1Finding(
            check="typecheck",
            passed=False,
            status=GateStatus.ERROR,
            detail=f"typecheck command not found: {' '.join(cfg.typecheck_cmd)}",
            summary="typecheck command not found (exit 127)",
        )
    passed = result.ok
    n = len(error_lines)
    summary = f"{n} type error(s)" if n else "no type errors"
    if result.timed_out:
        summary = f"timed out after {timeout}s"
        passed = False
        status = GateStatus.ERROR
    else:
        status = GateStatus.PASS if passed else GateStatus.FAIL
    detail = summary + "\n" + _tail(result.stdout or result.stderr)
    return L1Finding(
        check="typecheck", passed=passed, status=status, detail=detail, summary=summary
    )


_PYTEST_SUMMARY_RE = re.compile(
    r"(?P<passed>\d+) passed"
    r"(?:, (?P<failed>\d+) failed)?"
    r"(?:, (?P<errors>\d+) error)?",
)


def test_check(executor: SandboxExecutor, cfg: L1Config) -> L1Finding:
    if not cfg.test_cmd:
        return _not_configured("test", cfg)
    timeout = cfg.timeout_for("test")
    result = _run(executor, cfg.test_cmd, timeout)
    if result.returncode == 127:
        return L1Finding(
            check="test",
            passed=False,
            status=GateStatus.ERROR,
            detail=f"test command not found: {' '.join(cfg.test_cmd)}",
            summary="test command not found (exit 127)",
        )
    m = _PYTEST_SUMMARY_RE.search(result.stdout) or _PYTEST_SUMMARY_RE.search(result.stderr)
    # `m.group(0)` comes from the test output, but the pattern admits only
    # digits and the fixed words "passed"/"failed"/"error" — it cannot carry
    # arbitrary text out of the run, which is what makes it safe for `summary`.
    counts = m.group(0) if m else None
    passed = result.ok
    if result.timed_out:
        summary = f"timed out after {timeout}s running tests — L1 fails clean (P6)"
        detail = f"timed out after {timeout}s running tests — L1 fails clean (P6), no truncation"
        status = GateStatus.ERROR
    elif counts:
        summary = f"summary: {counts}"
        detail = summary
        status = GateStatus.PASS if passed else GateStatus.FAIL
    else:
        summary = f"exit code {result.returncode} (no pytest summary found in the output)"
        detail = summary
        status = GateStatus.PASS if passed else GateStatus.FAIL
    detail += "\n" + _tail(result.stdout or result.stderr)
    return L1Finding(
        check="test", passed=passed, status=status, detail=detail, summary=summary
    )


def build_check(executor: SandboxExecutor, cfg: L1Config) -> L1Finding:
    if not cfg.build_cmd:
        return _not_configured("build", cfg)
    timeout = cfg.timeout_for("build")
    result = _run(executor, cfg.build_cmd, timeout)
    if result.returncode == 127:
        return L1Finding(
            check="build",
            passed=False,
            status=GateStatus.ERROR,
            detail=f"build command not found: {' '.join(cfg.build_cmd)}",
            summary="build command not found (exit 127)",
        )
    passed = result.ok
    summary = "build ok" if passed else f"build failed (exit={result.returncode})"
    if result.timed_out:
        summary = f"timed out after {timeout}s"
        status = GateStatus.ERROR
    else:
        status = GateStatus.PASS if passed else GateStatus.FAIL
    detail = summary + "\n" + _tail(result.stdout or result.stderr)
    return L1Finding(
        check="build", passed=passed, status=status, detail=detail, summary=summary
    )
