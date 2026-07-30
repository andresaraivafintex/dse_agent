"""How the Tester bridge on K8s reports an ending it did not measure.

Three real incidents behind this file:
  - 2026-07-29, audit_log 23474: `tester_turn_completed` with returncode=137.
    A SIGKILL from the cgroup OOM killer went back to the Coder as a failing
    assertion, and the Coder spent a paid turn fixing a test that never ran.
  - the same bridge's `subprocess.run` calls had no `TimeoutExpired` handler:
    on expiry the activity returned NOTHING, so Temporal retried the whole turn
    — cold `npm install` included — with no result anywhere saying why.
  - `TesterTurnResult.cost_usd` was the literal 0.0, so the per-work-item
    ceiling counted only the Coder.

Everything here drives the real `_tester_pod_sync` with `subprocess.run`
replaced by a fake cluster, and asserts on the RESULT and the audit row — the
two surfaces the workflow and a human actually read.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from dse_contracts import GateStatus, RunTesterTurnInput
from sandbox_runtime import activities

_SUITE_MARKERS = ("npm test", "python3 -m pytest")
_REUSE_MARKER = "--grep='^tester('"

# Kept before any monkeypatching so a test can still shell out for real.
_REAL_RUN = subprocess.run


def _done(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def _fake_cluster(*, suite, reused=("tests/test_dse.py",), cp=None, seen=None):
    """Stands in for every `kubectl` the bridge shells out to. `suite` is what
    the suite run returns (a CompletedProcess) or raises (an exception); `cp`
    does the same for the `kubectl cp` of the workspace."""

    def fake_run(argv, **kwargs):
        if seen is not None:
            # The kwargs too: `timeout` is the clock that decides whether an
            # overrun is named `exec_timeout` or `suite_hung`.
            seen.append((argv, kwargs))
        joined = " ".join(argv)
        if "cp" in argv[:3]:
            local_ws = argv[-1]
            if isinstance(cp, BaseException):
                raise cp
            # A real `kubectl cp` leaves the workspace behind; the bridge checks
            # for its .git before it bothers to author anything.
            os.makedirs(os.path.join(local_ws, ".git"), exist_ok=True)
            return cp if cp is not None else _done(argv, 0)
        if _REUSE_MARKER in joined:
            return _done(argv, 0, stdout="".join(f"{f}\n" for f in reused))
        if any(m in joined for m in _SUITE_MARKERS):
            if isinstance(suite, BaseException):
                raise suite
            return suite
        # git config / git add+commit / git rev-parse / the local `git show`
        # that feeds the authoring prompt.
        return _done(argv, 0, stdout="deadbeef\n")

    return fake_run


@pytest.fixture()
def audits(monkeypatch):
    """audit_emit talks to Postgres; capture it and read the row instead."""
    rows: list[dict] = []
    monkeypatch.setattr(
        activities, "audit_emit", lambda **kw: rows.append(kw)
    )
    return rows


def _run_bridge(monkeypatch, *, seen=None, **cluster):
    monkeypatch.setattr(subprocess, "run", _fake_cluster(seen=seen, **cluster))
    return activities._tester_pod_sync(
        RunTesterTurnInput(work_item_id="wi-137", tenant_id="tenant-t", instruction="cover it"),
        "dse-sbx-wi-137",
        None,
        "vk-fixture",
        False,
    )


def _completed_row(audits):
    return next(a for a in audits if a["action"] == "tester_turn_completed")


def _suite_call(seen):
    """The `kubectl exec` that runs the suite: (argv, kwargs)."""
    return next(call for call in seen if "npm test" in call[0][-1])


# ---------------------------------------------------------------------------
# 0.3 — rc=137 (OOM kill) is not a failing assertion
# ---------------------------------------------------------------------------


def test_oom_kill_is_not_reported_as_a_test_failure(monkeypatch, audits):
    monkeypatch.setenv("DSE_SANDBOX_MEM_LIMIT", "1536Mi")
    result = _run_bridge(monkeypatch, suite=_done([], 137, stdout="ok 1 - adds\n"))

    assert result.returncode == 137
    assert result.tests_passed is False
    # FAIL is a verdict on the code under test. This one never got that far.
    assert result.status is GateStatus.ERROR
    assert "NOT A TEST FAILURE" in result.failure_output
    assert "1536Mi" in result.failure_output, "the limit that was hit has to be in the text"
    assert _completed_row(audits)["details"]["outcome"] == "resource_kill"


def test_the_memory_limit_is_named_even_when_the_env_does_not_carry_it(monkeypatch, audits):
    """A message that says the suite went over "its limit" without naming the
    limit sends the reader to the same hunt the incident already cost."""
    monkeypatch.delenv("DSE_SANDBOX_MEM_LIMIT", raising=False)
    result = _run_bridge(monkeypatch, suite=_done([], 137))

    assert "DSE_SANDBOX_MEM_LIMIT" in result.failure_output


def test_a_kill_and_a_timeout_are_kept_apart(monkeypatch, audits):
    """124 is a clock, 137 is a memory limit; merging them would send the Coder
    to close a handle when what it has to do is use less memory."""
    result = _run_bridge(monkeypatch, suite=_done([], 137))

    assert _completed_row(audits)["details"]["suite_hung"] is False
    assert "DID NOT TERMINATE" not in result.failure_output


def test_a_failing_assertion_is_still_a_plain_test_failure(monkeypatch, audits):
    """The whole point is the distinction — a real rc=1 must not acquire an
    infra excuse."""
    result = _run_bridge(monkeypatch, suite=_done([], 1, stdout="AssertionError: 1 != 2\n"))

    assert result.status is GateStatus.FAIL
    assert _completed_row(audits)["details"]["outcome"] == "tests_failed"
    assert result.failure_output.startswith("AssertionError")


def test_a_passing_suite_stays_a_pass(monkeypatch, audits):
    result = _run_bridge(monkeypatch, suite=_done([], 0, stdout="1 passed\n"))

    assert result.tests_passed is True
    assert result.status is GateStatus.PASS
    assert result.failure_output == ""
    assert _completed_row(audits)["details"]["outcome"] == "passed"


# ---------------------------------------------------------------------------
# 0.3 — the timeout message may only claim what the timeout measured
# ---------------------------------------------------------------------------


def test_a_hung_suite_message_does_not_invent_the_cause(monkeypatch, audits):
    """The old text asserted an open http server or a pending timer as fact. A
    timeout cannot tell stuck from slow, and the Coder paid for turns spent
    hunting a handle that may never have existed."""
    result = _run_bridge(monkeypatch, suite=_done([], 124, stdout="ok 1 - adds\n"))

    assert _completed_row(audits)["details"]["outcome"] == "suite_hung"
    assert result.status is GateStatus.ERROR
    # The budget is configuration now, so the text has to name the one THIS run
    # was measured against — and the ledger has to carry the same number, or a
    # `suite_hung` a week old cannot be told from a ConfigMap that was too tight.
    effective = activities._tester_clocks().suite
    assert f"{effective}s" in result.failure_output
    assert _completed_row(audits)["details"]["suite_timeout_seconds"] == effective
    assert "SLOW" in result.failure_output and "STUCK" in result.failure_output
    assert "may well pass" not in result.failure_output


# ---------------------------------------------------------------------------
# 0.2 — a TimeoutExpired has to become a result, never an escaped exception
# ---------------------------------------------------------------------------


def test_the_suite_exec_timing_out_returns_a_named_result(monkeypatch, audits):
    """Uncaught, this returned NO result at all: Temporal retried the turn from
    zero and re-ran the cold npm install, over and over."""
    boom = subprocess.TimeoutExpired(cmd=["kubectl", "exec"], timeout=600, output=b"partial\n")
    result = _run_bridge(monkeypatch, suite=boom)

    assert result.returncode == activities._RC_POD_EXEC_TIMEOUT
    assert result.tests_passed is False
    assert result.status is GateStatus.ERROR
    assert _completed_row(audits)["details"]["outcome"] == "exec_timeout"
    # the command and the limit that blew, not just "it failed". The limit is
    # the exec's, which is derived from the two configured budgets — reporting
    # the suite's here would point the reader at the wrong clock.
    assert f"{activities._tester_clocks().pod_exec}s" in result.failure_output
    assert "kubectl" in result.failure_output
    assert "partial" in result.failure_output, "whatever the process printed is evidence"


def test_a_timeout_with_no_output_at_all_is_still_readable(monkeypatch, audits):
    """TimeoutExpired.stdout/stderr are None when nothing was read before the
    kill, and bytes when the pipe was binary — neither may blow up the report."""
    boom = subprocess.TimeoutExpired(cmd=["kubectl", "exec"], timeout=600)
    result = _run_bridge(monkeypatch, suite=boom)

    assert result.returncode == activities._RC_POD_EXEC_TIMEOUT
    assert f"TIMEOUT after {activities._tester_clocks().pod_exec}s" in result.failure_output


def test_the_workspace_copy_timing_out_does_not_lose_the_turn(monkeypatch, audits):
    """`kubectl cp` of a real node_modules is what blows the 180s first. It used
    to escape the same way, and it took the whole turn with it."""
    boom = subprocess.TimeoutExpired(cmd=["kubectl", "cp"], timeout=180)
    result = _run_bridge(monkeypatch, suite=_done([], 0), reused=(), cp=boom)

    assert result.tests_ran is False, "nothing could be authored without the workspace"
    copy_failed = next(a for a in audits if a["action"] == "tester_workspace_copy_failed")
    assert copy_failed["details"]["returncode"] == activities._RC_POD_EXEC_TIMEOUT
    assert "180s" in copy_failed["details"]["stderr"]


def test_a_broken_npm_install_says_so_instead_of_hiding_in_dev_null(monkeypatch, audits):
    """The install ran under `|| true` with its output in /dev/null, so a
    half-installed node_modules reached the Coder as MODULE_NOT_FOUND — an
    import the runner broke, not the code. The script only ever executes in a
    Pod, so this checks the exact string that would be sent there, and parses
    it for real (`sh -n`) because it is built by concatenation and a quoting
    slip would only show up live."""
    seen: list[tuple[list[str], dict]] = []
    _run_bridge(monkeypatch, suite=_done([], 0), seen=seen)
    script = _suite_call(seen)[0][-1]

    assert "/dev/null" not in script
    assert "npm install FAILED" in script
    assert "not a test problem" in script
    # It also has a clock of its own now — without one it silently ate the
    # suite's share of the exec, and rc=124 had no meaning here.
    assert f"timeout -k 10 {activities._tester_clocks().install} npm install" in script
    parse = _REAL_RUN(["sh", "-n", "-c", script], capture_output=True, text=True)
    assert parse.returncode == 0, parse.stderr


# ---------------------------------------------------------------------------
# 0.1 — the suite's budget is configuration, and it has to REACH the Pod
# ---------------------------------------------------------------------------


def test_the_configured_budgets_are_the_ones_that_reach_the_pod(monkeypatch, audits):
    """Configuration that never reaches the shell is not configuration. This is
    the item: 180s was a module constant, the 180-600s band used to pass before
    it existed, and the exit criterion needs a real suite of 10 to 15 minutes."""
    monkeypatch.setenv("DSE_TESTER_SUITE_TIMEOUT_SECONDS", "840")
    monkeypatch.setenv("DSE_TESTER_INSTALL_TIMEOUT_SECONDS", "240")
    seen: list[tuple[list[str], dict]] = []
    _run_bridge(monkeypatch, suite=_done([], 0), seen=seen)
    argv, kwargs = _suite_call(seen)

    assert "timeout -k 10 840 npm test" in argv[-1]
    assert "timeout -k 10 240 npm install" in argv[-1]
    assert "timeout -k 10 180" not in argv[-1], "the old constant must be gone"
    # And the exec that wraps them is strictly outside both, or the overrun
    # would be reported as the worker's deadline instead of the suite's.
    assert kwargs["timeout"] > 840 + 240


def test_the_pytest_branch_gets_the_same_configured_budget(monkeypatch, audits):
    """The Python path hangs the same way and is the one a fix like this
    forgets — the acceptance repo for the SAST gate is Python."""
    monkeypatch.setenv("DSE_TESTER_SUITE_TIMEOUT_SECONDS", "840")
    seen: list[tuple[list[str], dict]] = []
    _run_bridge(monkeypatch, suite=_done([], 0), seen=seen)
    script = _suite_call(seen)[0][-1]

    assert "timeout -k 10 840 python3 -m pytest" in script


# ---------------------------------------------------------------------------
# The Tester's cost stops being the literal 0.0
# ---------------------------------------------------------------------------


class _FakeCompletion:
    def __init__(self, content: str, cost_usd: float):
        self.content = content
        self.cost_usd = cost_usd


def _fake_gateway(monkeypatch, content, cost_usd):
    from model_gateway_client import gateway_call

    monkeypatch.setattr(
        gateway_call, "chat_completion",
        lambda **kw: _FakeCompletion(content, cost_usd),
    )


_AUTHORED = json.dumps(
    {"files": [{"path": "tests/test_authored_dse.py", "content": "def test_x():\n    assert True\n"}]}
)


def test_the_turn_reports_what_the_authoring_call_cost(monkeypatch, audits):
    _fake_gateway(monkeypatch, _AUTHORED, 0.0123)
    result = _run_bridge(monkeypatch, suite=_done([], 0), reused=())

    assert result.test_files == ["tests/test_authored_dse.py"]
    assert result.cost_usd == pytest.approx(0.0123)
    assert _completed_row(audits)["details"]["cost_usd"] == pytest.approx(0.0123)


def test_an_unusable_answer_costs_exactly_as_much_as_a_usable_one(monkeypatch, audits):
    """The gateway billed the moment it answered. Reporting 0.0 because the JSON
    did not parse is how the money left both the ceiling and the reconciliation."""
    _fake_gateway(monkeypatch, "sorry, I cannot do that", 0.0077)
    result = _run_bridge(monkeypatch, suite=_done([], 0), reused=())

    assert result.tests_ran is False
    assert result.cost_usd == pytest.approx(0.0077)


def test_reusing_the_previous_round_costs_nothing(monkeypatch, audits):
    """No model call, no cost — the guard against a cost that appears from
    nowhere on a retry."""
    result = _run_bridge(monkeypatch, suite=_done([], 0))

    assert result.test_files == ["tests/test_dse.py"]
    assert result.cost_usd == 0.0
