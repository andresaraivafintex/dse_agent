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

#: How many of the lines a gate ACTUALLY COUNTED are reproduced in `detail`.
#: These are the evidence for the verdict, so they get their own budget ahead of
#: the raw tail rather than competing with it.
_MAX_ATTRIBUTED_LINES = 60


def _tail(text: str, max_lines: int = _MAX_DETAIL_LINES) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    omitted = len(lines) - max_lines
    return "\n".join(lines[-max_lines:]) + f"\n... ({omitted} earlier line(s) omitted)"


def _output(result: ExecResult) -> str:
    """Both streams, because the diagnosis is rarely on the one `or` picks.

    This was `result.stdout or result.stderr`, which discards stderr entirely
    whenever stdout is non-empty — and for a Node toolchain stdout is NEVER
    empty. Measured on the Angular testbed for one `npx jest --ci` run:

        stdout  24,610 lines  — captured console.* from tests, coverage table
        stderr   7,074 lines  — the PASS/FAIL headers, the `-` failure blocks,
                                and the "Test Suites: 2 failed, 275 passed" line

    So every `detail` ever written for that gate was 40 lines of the coverage
    table, and the failure blocks naming the broken suite were thrown away at
    write time. Raising `_MAX_DETAIL_LINES` could not have fixed it at any
    value: the answer was never in the stream being tailed. The same `or` sent
    `build`'s detail the `ng build` bundle-size table while the compiler error
    went to stderr.
    """
    return "\n".join(part for part in (result.stdout, result.stderr) if part)


def _detail_with(summary: str, attributed: list[str], result: ExecResult) -> str:
    """Lead with the lines the gate counted; keep the raw tail behind them.

    `detail` used to be `summary + _tail(output)`, so the only evidence anyone
    received was the END of the tool's output. On the Angular testbed that meant
    an operator reading a `typecheck` failure saw the alphabetical tail of a
    262-error dump — pre-existing errors in files the change never opened — with
    24,569 lines omitted, while the three errors that actually failed the gate
    sat in the omitted head.

    That is not a cosmetic problem. `workflows.py::_l1_failure_context` feeds
    this exact string to the NEXT Coder turn. Handed other people's errors, the
    Coder produced a byte-identical diff across four paid rounds — a measured
    delta of zero. The gate has to show its own evidence for the fix loop to be
    a loop at all.
    """
    parts = [summary]
    if attributed:
        shown = attributed[:_MAX_ATTRIBUTED_LINES]
        parts.append(f"--- the {len(attributed)} line(s) this gate counted ---")
        parts.extend(shown)
        if len(attributed) > len(shown):
            parts.append(f"... ({len(attributed) - len(shown)} more not shown)")
        parts.append("--- raw output (tail) ---")
    parts.append(_tail(_output(result)))
    return "\n".join(parts)


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


#: The two diagnostic shapes these gates parse:
#:   ruff/eslint  src/a.py:12:5: F401 msg
#:   tsc          src/a.ts(690,48): error TS2345: msg
_DIAGNOSTIC_PATH_RE = re.compile(r"^\s*(?P<path>[^\s:(]+)\s*[(:]")


def _path_of(line: str) -> str:
    """The file a diagnostic line points at, or "" when it points at none."""
    m = _DIAGNOSTIC_PATH_RE.match(line)
    return m.group("path").lstrip("./") if m else ""


def _only_in_changed_files(lines: list[str], changed_files: set[str] | None) -> list[str]:
    """Keep the diagnostics that land in files THIS CHANGE touched.

    The gate exists to judge the work item's diff, not the repository's history.
    Measured on the Angular testbed: `tsc --noEmit` reported 262 errors, every
    one of them in a `.spec.ts` the DSE never opened, against a change that
    added a single CONTRIBUTING.md. A markdown file cannot introduce TS2345 —
    but the gate failed the work item for them, the fix loop sent a paid Coder
    turn to repair someone else's specs, and no number of rounds could ever
    make a repository with pre-existing debt pass.

    `changed_files` of None means "no diff available" and everything counts:
    losing a real finding is worse than reporting one that is not ours."""
    if changed_files is None:
        return lines
    return [ln for ln in lines if _path_of(ln) in changed_files]


def _gate_is_unreachable(check: str, changed_files: set[str] | None) -> str | None:
    """Why this gate needs no run, or None if it does.

    The predicate itself lives in `dse_contracts.paths` because the workflow
    needs the SAME answer to decide whether the Tester has anything to test —
    and it turned out to be load-bearing that they agree. The Tester used to
    run unconditionally, author a spec, and have that spec committed into the
    diff, which made the change non-documentation-only and re-armed all four
    gates. This skip could therefore never fire while the Tester ran; the two
    decisions have to be taken from one list."""
    from dse_contracts.paths import is_documentation_only

    if not is_documentation_only(changed_files):
        return None
    return f"the change touches documentation only ({len(changed_files)} file(s))"


def _not_applicable(check: str, why: str) -> L1Finding:
    """A gate that did not need to run, recorded as such rather than silently
    absent. It PASSES because its answer is known, and the summary says why —
    an operator must never have to guess whether a gate ran."""
    return L1Finding(
        check=check,
        passed=True,
        status=GateStatus.PASS,
        detail=f"not run: {why}",
        summary=f"not run: {why}",
    )


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


def lint_check(
    executor: SandboxExecutor, cfg: L1Config, changed_files: set[str] | None = None
) -> L1Finding:
    if not cfg.lint_cmd:
        return _not_configured("lint", cfg)
    if (why := _gate_is_unreachable("lint", changed_files)) is not None:
        return _not_applicable("lint", why)
    # `timeout_for`, not `timeout_seconds`: the manifest's per-stage `timeouts`
    # block is the clock that was validated against the activity's budget, and
    # the number in the message below has to be the one that actually ran.
    timeout = cfg.timeout_for("lint")
    result = _run(executor, cfg.lint_cmd, timeout)
    # ruff/flake8: 1 line per issue, formatted as "path:line:col: CODE msg".
    issue_lines = [ln for ln in result.stdout.splitlines() if re.match(r"^\S+:\d+:\d+:\s", ln)]
    all_issues = len(issue_lines)
    issue_lines = _only_in_changed_files(issue_lines, changed_files)
    # `result.ok` is dropped from the verdict ON PURPOSE when the diff is known:
    # the command exits non-zero for the whole repository's issues, and this
    # gate answers a narrower question — did OUR change introduce any?
    passed = (result.ok or changed_files is not None) and len(issue_lines) == 0
    if result.timed_out:
        detail = f"timed out after {timeout}s running {' '.join(cfg.lint_cmd)}"
        summary = f"timed out after {timeout}s"
        status = GateStatus.ERROR
    elif (infra := _infra_failure(result)) is not None:
        detail = f"lint: {infra}\n" + _tail(_output(result))
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
            summary = (
                f"{n} lint issue(s) in the files this change touched"
                if changed_files is not None
                else f"{n} lint issue(s)"
            )
        elif passed:
            summary = (
                f"no lint issues in the files this change touched "
                f"({all_issues} elsewhere in the repository, not this change's)"
                if changed_files is not None and all_issues
                else "no lint issues"
            )
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
        detail = _detail_with(summary, issue_lines, result)
        status = GateStatus.PASS if passed else GateStatus.FAIL
    return L1Finding(
        check="lint", passed=passed, status=status, detail=detail, summary=summary
    )


#: `tsc` diagnostics: "src/a.ts(690,48): error TS2345: ...".
_TSC_ERROR_RE = re.compile(r": error TS\d+:")


def typecheck_check(
    executor: SandboxExecutor, cfg: L1Config, changed_files: set[str] | None = None
) -> L1Finding:
    if not cfg.typecheck_cmd:
        return _not_configured("typecheck", cfg)
    if (why := _gate_is_unreachable("typecheck", changed_files)) is not None:
        return _not_applicable("typecheck", why)
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
            detail=f"typecheck: {infra}\n" + _tail(_output(result)),
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
    all_errors = len(error_lines)
    error_lines = _only_in_changed_files(error_lines, changed_files)
    # Same reasoning as lint: with a diff in hand the question is not "does this
    # repository typecheck" — it is "did this change break the typecheck".
    passed = (result.ok or changed_files is not None) and not error_lines
    n = len(error_lines)
    if n:
        summary = (
            f"{n} type error(s) in the files this change touched"
            if changed_files is not None
            else f"{n} type error(s)"
        )
    elif passed:
        summary = (
            f"no type errors in the files this change touched "
            f"({all_errors} elsewhere in the repository, not this change's)"
            if changed_files is not None and all_errors
            else "no type errors"
        )
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
    detail = _detail_with(summary, error_lines, result)
    return L1Finding(
        check="typecheck", passed=passed, status=status, detail=detail, summary=summary
    )


#: One "<n> <word>" pair from a runner's count line. The alternation is closed,
#: so only digits and these fixed words can ever leave this parser — which is
#: what keeps the result safe for `summary` and therefore for the append-only
#: ledger (see `L1Finding.summary`).
_COUNT_RE = re.compile(r"(?P<n>\d+) (?P<word>passed|failed|errors?|skipped|todo)\b")

#: Words that mean the run has something wrong in it.
_BAD_WORDS = ("failed", "error", "errors")


def _test_counts(text: str) -> str | None:
    """The counts a test runner printed, in whatever order it printed them.

    This replaced a pattern that required `passed` FIRST with `failed` optional
    after it — pytest's order (`272 passed, 3 failed`). Jest writes the opposite
    (`Test Suites: 2 failed, 275 passed, 277 total`), so the optional group
    never matched and the failure count was silently dropped. A jest run with
    two broken suites published the byte-identical summary to a green one:

        'Test Suites: 2 failed, 275 passed, 277 total'  ->  '275 passed'

    That string is the only thing that propagates. It goes into the append-only
    `audit_log`, and `workflows.py::_l1_failure_context` hands it to the next
    Coder turn — so the fix loop was told to repair a suite it was told passed.
    Two days of debugging went into the gate before anyone read this regex.

    A line naming a non-zero failure wins outright; otherwise the first line
    carrying counts is used, so a green run still reads the way it always did.
    """
    fallback: str | None = None
    for line in text.splitlines():
        pairs = _COUNT_RE.findall(line)
        if not pairs:
            continue
        rendered = ", ".join(f"{n} {word}" for n, word in pairs)
        if any(word.startswith(_BAD_WORDS) and int(n) > 0 for n, word in pairs):
            return rendered
        if fallback is None:
            fallback = rendered
    return fallback


def _names_a_failure(counts: str) -> bool:
    return any(
        word.startswith(_BAD_WORDS) and int(n) > 0 for n, word in _COUNT_RE.findall(counts)
    )


#: Lines a runner uses to head a broken suite. These carry file paths, which is
#: why they are only ever used for `detail` (which lives in `validation_runs`
#: and is subject to retention) and never for `summary`.
_FAILING_SUITE_RE = re.compile(r"^\s*(?:FAIL|FAILED|ERROR)\b.*$")


def test_check(
    executor: SandboxExecutor, cfg: L1Config, changed_files: set[str] | None = None
) -> L1Finding:
    if not cfg.test_cmd:
        return _not_configured("test", cfg)
    if (why := _gate_is_unreachable("test", changed_files)) is not None:
        return _not_applicable("test", why)
    timeout = cfg.timeout_for("test")
    result = _run(executor, cfg.test_cmd, timeout)
    if (infra := _infra_failure(result)) is not None:
        return L1Finding(
            check="test",
            passed=False,
            status=GateStatus.ERROR,
            detail=f"test: {infra}\n" + _tail(_output(result)),
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
    output = _output(result)
    # Both streams: jest prints its counts and its failure blocks to STDERR,
    # and the old `stdout or stderr` never reached stderr at all.
    counts = _test_counts(output)
    passed = result.ok
    if result.timed_out:
        summary = f"timed out after {timeout}s running tests — L1 fails clean (P6)"
        detail = f"timed out after {timeout}s running tests — L1 fails clean (P6), no truncation"
        status = GateStatus.ERROR
    elif counts:
        summary = f"summary: {counts}"
        if not passed and not _names_a_failure(counts):
            # Every test passed and the command still failed. That is not a
            # contradiction and it is not ours to hide: the runner is enforcing
            # something besides the tests — a global coverage threshold, a
            # lint-in-test step, `errorOnDeprecated`. Saying only "275 passed"
            # beside a FAIL is the ledger asserting the opposite of the verdict
            # next to it, and it sends the fix loop hunting a broken test that
            # does not exist.
            summary += (
                f" — no test failed, but the command exited {result.returncode}: "
                "the suite's own policy rejected the run (coverage threshold or "
                "similar), not a failing test — see the detail"
            )
        status = GateStatus.PASS if passed else GateStatus.FAIL
    else:
        summary = f"exit code {result.returncode} (no test count found in the output)"
        status = GateStatus.PASS if passed else GateStatus.FAIL
    failing = [ln for ln in output.splitlines() if _FAILING_SUITE_RE.match(ln)]
    detail = _detail_with(summary, [] if passed else failing, result)
    return L1Finding(
        check="test", passed=passed, status=status, detail=detail, summary=summary
    )


def build_check(
    executor: SandboxExecutor, cfg: L1Config, changed_files: set[str] | None = None
) -> L1Finding:
    if not cfg.build_cmd:
        return _not_configured("build", cfg)
    if (why := _gate_is_unreachable("build", changed_files)) is not None:
        return _not_applicable("build", why)
    timeout = cfg.timeout_for("build")
    result = _run(executor, cfg.build_cmd, timeout)
    if (infra := _infra_failure(result)) is not None:
        return L1Finding(
            check="build",
            passed=False,
            status=GateStatus.ERROR,
            detail=f"build: {infra}\n" + _tail(_output(result)),
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
    detail = _detail_with(summary, [], result)
    return L1Finding(
        check="build", passed=passed, status=status, detail=detail, summary=summary
    )
