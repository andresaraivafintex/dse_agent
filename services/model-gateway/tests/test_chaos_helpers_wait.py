"""The chaos harness itself: the failover/recovery waits and the crash-safe
record of which containers we stopped, with Docker and the gateway faked out.

These cover the harness half of the intermittent test_failover_intra_tier.py
failure ("fallback never took over ... last api_base='...echo:9000'"): the old
loop swallowed every transport error, reported a single stale header value, and
could report a verdict without having made a single probe. They also cover the
budget guard and the container-restore record, which exist because the suite's
per-test ceiling (`--timeout-method=thread`) kills the process instead of failing
the test, skipping the `finally` that restarts the container.

No Docker, no proxy, no Postgres: `raw_completion` and `docker` are replaced.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import httpx
import pytest

from . import chaos_helpers
from .chaos_helpers import FALLBACK_API_BASE, PRIMARY_API_BASE


def _response(status: int, api_base: str | None, attempted: str = "0") -> httpx.Response:
    headers = {chaos_helpers.ATTEMPTED_FALLBACKS_HEADER: attempted}
    if api_base is not None:
        headers[chaos_helpers.MODEL_API_BASE_HEADER] = api_base
    return httpx.Response(status, headers=headers)


def _fake_probes(monkeypatch, outcomes):
    """Feeds `raw_completion` a scripted sequence; an Exception instance in the
    sequence is raised instead of returned, and the last outcome repeats once the
    sequence runs out. Records the contents probed."""
    remaining = list(outcomes)
    seen: list[str] = []

    def fake(content, *, model=chaos_helpers.ECHO_MODEL, key=None, timeout=None):
        seen.append(content)
        outcome = remaining.pop(0) if remaining else outcomes[-1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(chaos_helpers, "raw_completion", fake)
    return seen


# --- the waits ---------------------------------------------------------------
# `probe_timeout=0.0` in these tests: the fakes answer instantly, so the value
# only matters where a test is about the budget arithmetic itself.


def test_returns_as_soon_as_the_fallback_answers(monkeypatch):
    seen = _fake_probes(monkeypatch, [_response(200, FALLBACK_API_BASE, "1")])
    chaos_helpers.wait_until_fallback_serves(timeout_seconds=5.0, probe_timeout=0.0)
    assert len(seen) == 1


def test_returns_when_the_fallback_only_takes_over_on_a_later_probe(monkeypatch):
    """The realistic sequence: the first probe is cut off mid-flight, the second
    catches the router mid-failover, the third is served by echo-b."""
    seen = _fake_probes(
        monkeypatch,
        [
            httpx.ReadTimeout("probe outlived its client timeout"),
            _response(500, None),
            _response(200, FALLBACK_API_BASE, "1"),
        ],
    )
    chaos_helpers.wait_until_fallback_serves(timeout_seconds=30.0, probe_timeout=0.0)
    assert len(seen) == 3


def test_the_primary_still_serving_is_not_accepted_as_takeover(monkeypatch):
    _fake_probes(monkeypatch, [_response(200, PRIMARY_API_BASE)])
    with pytest.raises(AssertionError) as ei:
        chaos_helpers.wait_until_fallback_serves(timeout_seconds=0.0, probe_timeout=0.0)
    assert "fallback never took over" in str(ei.value)


def test_a_non_200_from_the_fallback_is_not_accepted_as_takeover(monkeypatch):
    """The endpoint header alone is not enough — a 5xx that still names echo-b
    means the fallback did NOT serve the request."""
    _fake_probes(monkeypatch, [_response(503, FALLBACK_API_BASE, "1")])
    with pytest.raises(AssertionError):
        chaos_helpers.wait_until_fallback_serves(timeout_seconds=0.0, probe_timeout=0.0)


def test_at_least_one_probe_is_made_even_with_an_exhausted_budget(monkeypatch):
    """The old `while monotonic() < deadline` could report a verdict without ever
    asking the gateway anything, which is how the failure message ended up
    carrying a value no probe had observed."""
    seen = _fake_probes(monkeypatch, [_response(200, PRIMARY_API_BASE)])
    with pytest.raises(AssertionError):
        chaos_helpers.wait_until_fallback_serves(timeout_seconds=0.0, probe_timeout=0.0)
    assert len(seen) == 1


def test_probes_keep_being_started_for_the_whole_window(monkeypatch):
    """The window a wait quotes has to be the window it actually polls. A guard
    that refused to START a probe unable to FINISH inside the window cut the real
    polling to `window - probe_timeout - poll_interval` — 4s of a 30s budget for
    `ensure_primary_serving`, one single probe — while the AssertionError still
    said "within 30.0s". A container that took a few more seconds to accept
    connections then failed the wait, which is the flake this harness exists to
    remove. The in-flight tail is charged in WORST_CASE_*_SECONDS instead.

    A big `probe_timeout` with instant fakes is exactly the shape that used to
    collapse the window."""
    seen = _fake_probes(monkeypatch, [_response(200, PRIMARY_API_BASE)])
    started = time.monotonic()
    with pytest.raises(AssertionError) as ei:
        chaos_helpers._wait_until_served_by(
            FALLBACK_API_BASE,
            content="healthcheck-fallback",
            failure="fallback never took over",
            timeout_seconds=0.6,
            poll_interval=0.2,
            probe_timeout=30.0,
        )
    elapsed = time.monotonic() - started
    # probes at ~0.0/0.2/0.4 (the one at 0.4 is followed by 0.4+0.2 >= 0.6 -> stop)
    assert len(seen) >= 3, f"only {len(seen)} probe(s) in a 0.6s window polled every 0.2s"
    assert elapsed >= 0.4, f"gave up after {elapsed:.2f}s of a 0.6s window"
    message = str(ei.value)
    assert f"{len(seen)} probe(s) made" in message
    # the message has to say how long it really took, because the window it quotes
    # is the probe-START window and the last probe may outlive it
    assert "0.6s polling window" in message and "gave up after" in message, message


def test_failure_message_reports_the_probe_count_and_what_each_probe_saw(monkeypatch):
    """A red run must distinguish "the budget only bought N probes, all of them
    timing out" from "the router really kept announcing the dead primary"."""
    seen = _fake_probes(
        monkeypatch,
        [
            httpx.ReadTimeout("cut off mid-flight"),
            _response(200, PRIMARY_API_BASE, "0"),
        ],
    )
    with pytest.raises(AssertionError) as ei:
        chaos_helpers.wait_until_fallback_serves(timeout_seconds=1.2, probe_timeout=0.0)
    message = str(ei.value)
    assert len(seen) >= 2
    assert f"{len(seen)} probe(s) made" in message
    assert "ReadTimeout" in message
    assert PRIMARY_API_BASE in message
    assert FALLBACK_API_BASE in message  # the endpoint that was expected
    assert "attempted_fallbacks='0'" in message


def test_failure_message_spells_out_only_the_last_probes(monkeypatch):
    """Bounded output: a 40s budget against fast refusals produces hundreds of
    probes, and an unreadable assertion is as bad as no assertion. Each probe
    here reports a DIFFERENT api_base, so the message has to show the last
    _PROBES_IN_FAILURE_MESSAGE of them and drop the older ones."""
    cap = chaos_helpers._PROBES_IN_FAILURE_MESSAGE
    seen: list[str] = []

    def fake(content, *, model=chaos_helpers.ECHO_MODEL, key=None, timeout=None):
        seen.append(content)
        return _response(200, f"http://probe-{len(seen)}:9000")

    monkeypatch.setattr(chaos_helpers, "raw_completion", fake)
    with pytest.raises(AssertionError) as ei:
        # The private helper, to drive the poll interval to 0 and pile up far
        # more probes than the cap inside a fraction of a second.
        chaos_helpers._wait_until_served_by(
            FALLBACK_API_BASE,
            content="healthcheck-fallback",
            failure="fallback never took over",
            timeout_seconds=0.3,
            poll_interval=0.0,
            probe_timeout=0.0,
        )
    message = str(ei.value)
    total = len(seen)
    assert total > cap, f"only {total} probes: the cap was never exercised"
    assert f"{total} probe(s) made" in message
    assert message.count("status=200") == cap
    for i in range(total - cap + 1, total + 1):
        assert f"http://probe-{i}:9000" in message, f"probe {i} of {total} is missing"
    assert f"http://probe-{total - cap}:9000" not in message


def test_recovery_wait_requires_the_primary_and_reports_the_fallback(monkeypatch):
    """`ensure_primary_serving` must not be satisfied by the fallback: the whole
    point is to hand the next test a NON-degraded gateway."""
    _fake_probes(monkeypatch, [_response(200, FALLBACK_API_BASE, "1")])
    with pytest.raises(AssertionError) as ei:
        chaos_helpers.ensure_primary_serving(timeout_seconds=0.0, probe_timeout=0.0)
    message = str(ei.value)
    assert "primary never came back to serving" in message
    assert FALLBACK_API_BASE in message


def test_recovery_wait_returns_once_the_primary_answers(monkeypatch):
    seen = _fake_probes(
        monkeypatch,
        [_response(200, FALLBACK_API_BASE, "1"), _response(200, PRIMARY_API_BASE)],
    )
    chaos_helpers.ensure_primary_serving(timeout_seconds=30.0, probe_timeout=0.0)
    assert len(seen) == 2


def test_transport_errors_do_not_abort_the_wait(monkeypatch):
    """The proxy container itself can be restarting mid-wait; that is an
    observation, not a reason to fail the test with a transport error."""
    _fake_probes(
        monkeypatch,
        [httpx.ConnectError("proxy restarting"), _response(200, PRIMARY_API_BASE)],
    )
    chaos_helpers.ensure_primary_serving(timeout_seconds=30.0, probe_timeout=0.0)


# --- the crash-safe record of stopped containers -----------------------------


@pytest.fixture
def chaos_state_dir(monkeypatch, tmp_path):
    """Keeps the marker file out of the machine's real temp dir (where a shared
    entry would make the next real run restart containers it never stopped)."""
    monkeypatch.setenv("DSE_CHAOS_STATE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def dead_pid() -> int:
    """The pid of a process that has already exited and been reaped — what a
    session killed by the per-test ceiling leaves behind in the marker's name."""
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    assert not chaos_helpers._pid_alive(proc.pid), (
        f"pid {proc.pid} was reused between wait() and the liveness check"
    )
    return proc.pid


def test_pid_liveness_tells_a_running_session_from_a_finished_one(dead_pid):
    """The whole scoping rests on this call: a marker belonging to a live pid must
    be left alone, one belonging to a dead pid must be repaired."""
    assert chaos_helpers._pid_alive(os.getpid())
    assert not chaos_helpers._pid_alive(dead_pid)


def test_stopping_a_container_records_it_and_starting_it_clears_the_record(
    monkeypatch, chaos_state_dir
):
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(chaos_helpers, "docker", lambda *args: calls.append(args))

    chaos_helpers.stop_container("dse_model_gateway_echo")
    assert chaos_helpers._recorded_stopped() == ["dse_model_gateway_echo"]

    chaos_helpers.start_container("dse_model_gateway_echo")
    assert chaos_helpers._recorded_stopped() == []
    assert calls == [
        ("stop", "dse_model_gateway_echo"),
        ("start", "dse_model_gateway_echo"),
    ]


def test_the_record_is_written_before_the_container_is_stopped(monkeypatch, chaos_state_dir):
    """Order matters: the process can be killed at any instant. Recorded first, a
    kill costs one redundant `docker start`; recorded last, it costs a container
    left stopped with nothing on disk saying so."""

    def boom(*args: str) -> None:
        raise subprocess.CalledProcessError(1, ["docker", *args])

    monkeypatch.setattr(chaos_helpers, "docker", boom)
    with pytest.raises(subprocess.CalledProcessError):
        chaos_helpers.stop_container("dse_model_gateway_echo")
    assert chaos_helpers._recorded_stopped() == ["dse_model_gateway_echo"]


def test_the_next_session_restarts_what_an_aborted_run_left_stopped(
    monkeypatch, chaos_state_dir, dead_pid
):
    chaos_helpers._write_recorded_stopped(
        ["dse_model_gateway_echo", "dse_model_gateway_echo_b"],
        path=chaos_helpers._marker_path(dead_pid),
    )
    started: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        started.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(chaos_helpers.subprocess, "run", fake_run)
    restored = chaos_helpers.restore_containers_from_aborted_run()

    assert restored == ["dse_model_gateway_echo", "dse_model_gateway_echo_b"]
    assert started == [
        ["docker", "start", "dse_model_gateway_echo"],
        ["docker", "start", "dse_model_gateway_echo_b"],
    ]
    assert chaos_helpers._recorded_stopped(dead_pid) == []


def test_a_marker_left_by_a_still_running_session_is_not_touched(
    monkeypatch, chaos_state_dir
):
    """The repair used to `docker start` every container the machine-wide marker
    listed, so a session starting up restarted the primary echo that ANOTHER,
    still-running session had just stopped on purpose — that session's next call
    was then served by the endpoint it had taken down, and its failover assertions
    became a coin flip. Nothing is restored for a pid that still exists, this
    process's own included."""

    def fail(*args, **kwargs):
        raise AssertionError("docker must not be invoked for a live session's marker")

    chaos_helpers._write_recorded_stopped(["dse_model_gateway_echo"])  # our own, live pid
    monkeypatch.setattr(chaos_helpers.subprocess, "run", fail)
    assert chaos_helpers.restore_containers_from_aborted_run() == []
    # and the record survives, so the session that owns it can still clear it
    assert chaos_helpers._recorded_stopped() == ["dse_model_gateway_echo"]


def test_a_container_that_will_not_restart_stays_recorded(
    monkeypatch, chaos_state_dir, dead_pid
):
    """Clearing the record on a failed `docker start` would throw away the only
    evidence that the gateway is degraded."""
    chaos_helpers._write_recorded_stopped(
        ["dse_model_gateway_echo"], path=chaos_helpers._marker_path(dead_pid)
    )
    monkeypatch.setattr(
        chaos_helpers.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1, b"", b"no such container"),
    )
    assert chaos_helpers.restore_containers_from_aborted_run() == []
    assert chaos_helpers._recorded_stopped(dead_pid) == ["dse_model_gateway_echo"]


def test_nothing_recorded_runs_no_docker_command_at_all(monkeypatch, chaos_state_dir):
    """The repair runs at every session start, including on machines with no
    Docker and in suites that never touch a container. No marker file exists
    here, so not even the state directory is walked into a docker call."""

    def fail(*args, **kwargs):
        raise AssertionError("docker must not be invoked when nothing is recorded")

    monkeypatch.setattr(chaos_helpers.subprocess, "run", fail)
    assert chaos_helpers.restore_containers_from_aborted_run() == []
