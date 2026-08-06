"""The Tester answers its own question, not L1's.

Measured on the Angular testbed, one round of one work item:

    run_tester_turn   ~9 min   — of which ~400s is `npm test` over 4,975 tests
    run_l1_pipeline  ~10 min   — of which ~403s is the SAME 4,975 tests again

Two thirds of a 24-minute round was one suite, run twice, over the same
workspace in the same Pod. And the redundancy is already acknowledged in
`activities.py` beside the exec: "The suite result is INFORMATION here, not a
verdict. L1's `test` gate runs the same command, in the same Pod, over the same
workspace, minutes later — and it judges the COMMITTED state."

So the Tester runs only what it authored. Nothing is lost: L1 still runs the
whole suite, with coverage, over the committed tree, and L1 is the verdict.
"""
from __future__ import annotations

import pytest

from sandbox_runtime.activities import _npm_suite_command


def test_the_suite_is_scoped_to_the_files_the_tester_wrote():
    cmd = _npm_suite_command(["src/app/a.spec.ts", "src/app/b.spec.ts"])
    assert "src/app/a.spec.ts" in cmd
    assert "src/app/b.spec.ts" in cmd


def test_authoring_nothing_still_runs_the_whole_suite():
    """No authored file means nothing to scope to. Running everything is the
    only honest thing left, and it is what this did before."""
    assert _npm_suite_command([]) == "npm test --silent"


def test_a_scoped_run_turns_coverage_off():
    """Load-bearing, and the reason a naive scoping would make things WORSE.

    The testbed sets `collectCoverage: true` with global thresholds, so any
    subset misses them: measured at 9.83% statements against a floor of 80%
    with every single test passing. Scoping without this flag converts a fast
    check into a guaranteed red one, and sends the Coder after an arithmetic
    failure. L1 keeps running the full suite with coverage on."""
    cmd = _npm_suite_command(["src/app/a.spec.ts"])
    assert "--coverage=false" in cmd


def test_coverage_is_not_disabled_when_the_whole_suite_runs():
    """The flag exists to make a SUBSET viable. Carrying it into the full run
    would silently drop the repository's own coverage policy."""
    assert "--coverage=false" not in _npm_suite_command([])


def test_the_flags_reach_jest_and_not_npm():
    """`npm test --coverage=false` is an npm flag and jest never sees it. The
    `--` separator is what forwards the rest to the runner."""
    cmd = _npm_suite_command(["src/a.spec.ts"])
    sep = cmd.index(" -- ")
    assert cmd.index("--coverage=false") > sep
    assert cmd.index("src/a.spec.ts") > sep


@pytest.mark.parametrize(
    "path",
    [
        "src/app/a b.spec.ts",
        "src/app/$(rm -rf /).spec.ts",
        "src/app/it's.spec.ts",
        "src/app/a;b.spec.ts",
    ],
)
def test_a_hostile_path_cannot_escape_the_shell(path):
    """These paths come from a model's file list and are interpolated into a
    string that a shell runs inside the Pod."""
    cmd = _npm_suite_command([path])
    assert "rm -rf /" not in cmd.replace(_quoted(path), "")
    assert _quoted(path) in cmd


def _quoted(path: str) -> str:
    from shlex import quote

    return quote(path)
