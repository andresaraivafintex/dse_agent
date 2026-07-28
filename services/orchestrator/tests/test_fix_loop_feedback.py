"""The automated fix loop must tell the Coder what went wrong.

Real incident: a Slack task ("download a csv with the historic") failed four
times and hit the retry cap. The instruction sent to the Coder was byte-for-byte
identical on all four attempts — same md5, same 687 characters — because the
tester/L1 loop rebuilt it from the original request and appended nothing. The
last attempt changed no files at all: there was nothing new to act on.

The human-review loop had always appended its objections. These tests pin the
other loop to the same contract.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from dse_orchestrator.workflows import WorkItemLifecycleWorkflow as WF


@pytest.fixture(autouse=True)
def _require_postgres():
    """Overrides the conftest fixture of the same name.

    Everything here is pure string formatting over values already in hand, so it
    needs no database — and skipping it without one would hide exactly the kind
    of regression these tests exist to catch."""
    return None


class _Finding(SimpleNamespace):
    pass


def _tester(returncode=1, failure_output=""):
    return SimpleNamespace(returncode=returncode, failure_output=failure_output)


def test_the_suite_output_reaches_the_coder():
    ctx = WF._tester_failure_context(
        _tester(returncode=1, failure_output="AssertionError: expected 3 columns, got 2")
    )
    joined = "\n".join(ctx)
    assert "expected 3 columns, got 2" in joined, "the Coder never learns what failed"
    assert "1" in joined  # the exit code travels too


def test_the_coder_is_told_not_to_delete_the_tests():
    """The cheapest way to make a suite pass is to remove it. Say so."""
    joined = "\n".join(WF._tester_failure_context(_tester(failure_output="boom")))
    assert "do not weaken or delete the tests" in joined.lower()


def test_missing_output_is_reported_as_missing_not_as_silence():
    """An older worker returns no output. 'The suite printed nothing' and 'we
    failed to capture it' call for different fixes, so they must read
    differently."""
    joined = "\n".join(WF._tester_failure_context(_tester(returncode=7, failure_output="")))
    assert "not captured" in joined
    assert "7" in joined


def test_only_the_failing_l1_checks_are_reported():
    """`l1_completed` records every check that RAN. A real run had seven checks
    passing and only `sast` erroring, and the record made it look like all eight
    had failed — so the Coder was told to fix everything and fixed nothing."""
    ctx = WF._l1_failure_context([
        _Finding(check="sast", passed=False, status=None, detail="semgrep exited 2"),
    ])
    joined = "\n".join(ctx)
    assert "sast" in joined and "semgrep exited 2" in joined
    for clean in ("lint", "typecheck", "build", "secret_scan"):
        assert clean not in joined, f"{clean} passed and must not be reported as a failure"


def test_a_finding_without_detail_still_names_the_check():
    joined = "\n".join(WF._l1_failure_context([
        _Finding(check="build", passed=False, status=None, detail=""),
    ]))
    assert "build" in joined and "no detail reported" in joined


def test_nothing_failing_produces_no_context():
    assert WF._l1_failure_context([]) == []


def test_the_instruction_actually_grows_when_a_retry_is_pending():
    """The regression itself: same input, and the instruction must now differ
    between the first attempt and the retry."""
    wf = WF.__new__(WF)
    base = SimpleNamespace(
        task_content="create a feature that allow me to download a csv",
        acceptance_criteria="repo=andre2654/fintex-wallet",
        clarification_notes=[],
        l2_objections=[],
        fix_context=[],
    )
    wf._input = base
    first = wf._agent_instruction(include_objections=True)

    base.fix_context = WF._tester_failure_context(
        _tester(returncode=1, failure_output="AssertionError: expected 3 columns, got 2")
    )
    retry = wf._agent_instruction(include_objections=True)

    assert retry != first, "the retry repeats the original instruction verbatim"
    assert first in retry, "the original task must survive alongside the failure"
    assert "expected 3 columns, got 2" in retry
