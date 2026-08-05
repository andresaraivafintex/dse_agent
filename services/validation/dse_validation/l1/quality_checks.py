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


#: 128 + signal. 137 = SIGKILL (the cgroup's OOM killer inside a container),
#: 139 = SIGSEGV, 143 = SIGTERM.
#:
#: 134 = SIGABRT, and it is the one that actually showed up: V8 does not get
#: killed when it exhausts its heap, it prints "FATAL ERROR: ... JavaScript
#: heap out of memory" and calls abort(). Observed on the Angular testbed —
#: `ng lint` died with exit=134 after printing nothing but `Linting "..."`, and
#: the first version of this classifier missed it, so the gate still read as a
#: verdict on the customer's code. The marker text was on stderr and went with
#: the process, which is why the return code has to carry the diagnosis.
_KILLED_RETURNCODES = frozenset({134, 137, 139, 143})

#: What a Node toolchain prints on its way out of memory. `ng lint` and
#: `ng build` on a real Angular app are the two commands that reach it.
_OOM_MARKERS = (
    "javascript heap out of memory",
    "allocation failure",
    "fatal error: reached heap limit",
    "killed",
    "cannot allocate memory",
    "enomem",
)


def _infra_failure(result: ExecResult) -> str | None:
    """Names an INFRASTRUCTURE failure, or None if the tool merely disagreed
    with the code.

    Without this, a gate killed by the cgroup's OOM killer is scored
    `GateStatus.FAIL` — a verdict on the customer's code — because the only
    thing distinguishing it from real lint errors is a return code nobody
    looked at. The workflow then spends a paid Coder turn "fixing" a lint run
    that never produced a finding, three times, and the work item ends `failed`
    with a reason that blames the diff.

    The sandbox runs `ng lint` and `ng build` under a 1536Mi limit while V8
    sizes its heap from the NODE's memory, so this is not hypothetical: it is
    the same exhaustion that already killed this repository's checkpoint
    commits when husky ran the linter on them.

    The Tester path has had this distinction for a while
    (`_tester_infra_outcome`); L1 had none."""
    if result.returncode in _KILLED_RETURNCODES:
        return f"the process was killed (exit={result.returncode})"
    blob = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
    for marker in _OOM_MARKERS:
        if marker in blob:
            return "the process ran out of memory"
    return None


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
    elif (infra := _infra_failure(result)) is not None:
        detail = f"lint: {infra}\n" + _tail(result.stdout or result.stderr)
        summary = f"lint could not run: {infra}"
        passed = False
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
        if n:
            summary = f"{n} lint issue(s)"
        elif passed:
            summary = "no lint issues"
        else:
            # The linter rejected the tree but printed nothing in the
            # "path:line:col: CODE msg" shape this parser knows — eslint's
            # default formatter is one such. Saying "no lint issues" on a gate
            # that just FAILED is worse than saying nothing: it is the ledger
            # asserting the opposite of the verdict beside it. Observed on the
            # Angular testbed, where lint FAILED reading "no lint issues".
            summary = (
                f"lint failed (exit={result.returncode}); no issue line matched "
                "the expected 'path:line:col: CODE msg' format — see the detail"
            )
        detail = summary + "\n" + _tail(result.stdout or result.stderr)
        status = GateStatus.PASS if passed else GateStatus.FAIL
    return L1Finding(
        check="lint", passed=passed, status=status, detail=detail, summary=summary
    )


#: `tsc` diagnostics: "src/a.ts(690,48): error TS2345: ...".
_TSC_ERROR_RE = re.compile(r": error TS\d+:")


def typecheck_check(executor: SandboxExecutor, cfg: L1Config) -> L1Finding:
    if not cfg.typecheck_cmd:
        return _not_configured("typecheck", cfg)
    timeout = cfg.timeout_for("typecheck")
    result = _run(executor, cfg.typecheck_cmd, timeout)
    # `: error:` is mypy. `tsc` writes `path(line,col): error TS2345: ...`, so
    # matching only mypy's shape counted zero errors on every TypeScript repo
    # and the gate failed reporting "no type errors". This widening changes the
    # COUNT only — `passed` is the command's exit code either way.
    error_lines = [
        ln
        for ln in result.stdout.splitlines()
        if ": error:" in ln or _TSC_ERROR_RE.search(ln)
    ]
    if (infra := _infra_failure(result)) is not None:
        return L1Finding(
            check="typecheck",
            passed=False,
            status=GateStatus.ERROR,
            detail=f"typecheck: {infra}\n" + _tail(result.stdout or result.stderr),
            summary=f"typecheck could not run: {infra}",
        )
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
    if n:
        summary = f"{n} type error(s)"
    elif passed:
        summary = "no type errors"
    else:
        summary = (
            f"typecheck failed (exit={result.returncode}); no diagnostic line "
            "was recognised — see the detail"
        )
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
    if (infra := _infra_failure(result)) is not None:
        return L1Finding(
            check="test",
            passed=False,
            status=GateStatus.ERROR,
            detail=f"test: {infra}\n" + _tail(result.stdout or result.stderr),
            summary=f"the test suite could not run: {infra}",
        )
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
    if (infra := _infra_failure(result)) is not None:
        return L1Finding(
            check="build",
            passed=False,
            status=GateStatus.ERROR,
            detail=f"build: {infra}\n" + _tail(result.stdout or result.stderr),
            summary=f"the build could not run: {infra}",
        )
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
