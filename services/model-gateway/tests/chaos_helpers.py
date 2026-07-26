"""Shared helpers for WS-D's chaos/failover tests (Phase 3).

REAL chaos: we take down WS-D's own containers (`docker stop`) — never the
foundation's shared infra (Postgres/Temporal/Redis/Vault) nor other
workstreams' services. Any test that takes an echo down MUST restore it in
`finally` and wait for the primary to serve again (`ensure_primary_serving`) so
it does not leak degraded state to other tests/agents running in parallel.

`finally` is necessary but NOT sufficient: the suite runs under
`--timeout=180 --timeout-method=thread` (scripts/test_matrix.py), and that
method does not raise inside the test — pytest-timeout dumps the stacks and calls
`os._exit(1)`, which skips `finally` and `atexit` alike. So this module also
(a) keeps the worst case of each chaos test under that ceiling (see "Timing
budgets"), and (b) records each stop in a file named after THIS process, so a
later session can repair a container left stopped by a process that is gone
(`restore_containers_from_aborted_run`, wired in conftest.py) without touching
the containers of a session that is still running.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

PRIMARY_ECHO_CONTAINER = "dse_model_gateway_echo"
FALLBACK_ECHO_CONTAINER = "dse_model_gateway_echo_b"
PRIMARY_API_BASE = "http://model-gateway-echo:9000"
FALLBACK_API_BASE = "http://model-gateway-echo-b:9000"

ECHO_MODEL = "eco/echo-model"
ECHO_MODEL_B = "eco/echo-model-b"

MODEL_API_BASE_HEADER = "x-litellm-model-api-base"
ATTEMPTED_FALLBACKS_HEADER = "x-litellm-attempted-fallbacks"

# --- Timing budgets ---------------------------------------------------------
# Every number below is spent inside ONE pytest test, against a 180s per-test
# ceiling enforced with `--timeout-method=thread`, i.e. a ceiling that kills the
# process instead of failing the test. tests/test_router_calibration.py adds the
# numbers up against the ceiling it parses out of scripts/test_matrix.py, so a
# later bump here cannot silently cross it.

# What ONE request costs the proxy while ONE echo of the pair is stopped and the
# other is up: the dead deployment burns (1 + num_retries) attempts against its
# own `timeout: 5`, plus one retry back-off, and only THEN does the surviving
# group answer. Model: 2*5 + 1.25 = 11.25s (asserted in
# test_router_calibration.py); measured in this session: 11-13s, more on a
# contended runner. Model and measurement agree, so the numbers below are sized
# from this one.
#
# The same per-group model applied to BOTH groups dead predicts 22.5s, and this
# repository's own measurement of that state is ~53s (see
# TOTAL_OUTAGE_CALL_TIMEOUT_SECONDS). The model is therefore NOT an upper bound
# once every group is dead — the unmodelled time is in LiteLLM's give-up path —
# and it is not used to size anything here. No probe ever waits for that path: a
# probe waits for a LIVE endpoint to answer, and if none does it records the
# timeout and polls again. The only ceiling that has to cover the all-dead path is
# the total-outage test's own call timeout, and that one comes from the
# measurement, not from the model.

# Probe timeout for `wait_until_fallback_serves`. The answer it waits for arrives
# only AFTER the stopped primary group is burnt (11-13s above), so a shorter
# timeout abandons the very request that carries the answer and the poll learns
# nothing — which is what made test_failover_intra_tier.py intermittently red with
# the old 15s value. The old loop then swallowed the ReadTimeout WITHOUT recording
# it and kept reporting the api_base of an earlier probe, the reason the red run
# named the primary and looked like a router problem.
PROBE_TIMEOUT_SECONDS = 20.0

# Probe timeout for `ensure_primary_serving`. The answer IT waits for is a HEALTHY
# primary: `eco/echo-model` is the first deployment tried and it is a local
# uvicorn that answers in milliseconds, and the proxy abandons any attempt that
# outlives the deployment's own `timeout: 5` anyway. So a probe still unanswered
# after 8s is the observation "the primary is not serving yet", not an answer
# thrown away — and cheap probes are what let this wait poll often enough to
# notice the container coming back inside its window.
PRIMARY_RECOVERY_PROBE_TIMEOUT_SECONDS = 8.0

# For how long each wait keeps STARTING probes, and the interval between them.
# This window is what the failure message quotes, and it is spent polling: the
# only thing subtracted from it is one poll interval (a probe is not started when
# the wait is about to leave the window). A probe that HAS started always runs to
# completion, so a wait's wall-clock worst case is window + one probe timeout —
# charged in the WORST_CASE_* sums below rather than hidden.
PRIMARY_RECOVERY_TIMEOUT_SECONDS = 25.0
PRIMARY_RECOVERY_POLL_INTERVAL_SECONDS = 1.0
FALLBACK_TAKEOVER_TIMEOUT_SECONDS = 25.0
FALLBACK_TAKEOVER_POLL_INTERVAL_SECONDS = 0.5

# Client-side ceilings for the instrumented calls the chaos tests make while an
# echo is down. Sized from what the proxy actually costs in that state, NOT
# "generously", because they are part of the same 180s budget.
#   - one echo down: 11-13s measured (the number derived above).
#   - both echoes down: ~53s measured in this session — LiteLLM burns
#     connect-timeouts x retries x fallback before the final 5xx/408, and it
#     costs far more than the 22.5s the per-group model predicts, which is why
#     this ceiling follows the measurement.
FAILOVER_CALL_TIMEOUT_SECONDS = 25.0
MEASURED_BOTH_ECHOES_DOWN_ANSWER_SECONDS = 53.0
TOTAL_OUTAGE_CALL_TIMEOUT_SECONDS = 70.0

# Allowance for the `docker stop`/`docker start` steps of one test (up to two of
# each). A stop can take a few seconds while the container's network is torn down.
DOCKER_STEP_BUDGET_SECONDS = 20.0

# Worst case of ONE wait: its polling window plus the one probe that may still be
# in flight when the window ends.
_PRIMARY_RECOVERY_WORST_CASE_SECONDS = (
    PRIMARY_RECOVERY_TIMEOUT_SECONDS + PRIMARY_RECOVERY_PROBE_TIMEOUT_SECONDS
)
_FALLBACK_TAKEOVER_WORST_CASE_SECONDS = (
    FALLBACK_TAKEOVER_TIMEOUT_SECONDS + PROBE_TIMEOUT_SECONDS
)

# Worst case of the heaviest test in each chaos file, spelled out here so the
# arithmetic lives next to the numbers it is made of. Every wait is charged in
# FULL, including the pre-flight one: a pre-flight wait that spends its whole
# window can still SUCCEED (a primary that comes back late), so it does not
# exclude the steps after it.
# test_failover_intra_tier.py::test_primary_down_fallback_serves_...:
WORST_CASE_SINGLE_ECHO_DOWN_SECONDS = (
    _PRIMARY_RECOVERY_WORST_CASE_SECONDS  # pre-flight: primary must be serving
    + _FALLBACK_TAKEOVER_WORST_CASE_SECONDS  # wait for echo-b to take over
    + FAILOVER_CALL_TIMEOUT_SECONDS  # the instrumented call, under failover
    + _PRIMARY_RECOVERY_WORST_CASE_SECONDS  # restore, in the outer finally
    + DOCKER_STEP_BUDGET_SECONDS
)
# test_chaos_gateway.py::test_total_provider_outage_clean_refusal_audited:
WORST_CASE_BOTH_ECHOES_DOWN_SECONDS = (
    _PRIMARY_RECOVERY_WORST_CASE_SECONDS
    + TOTAL_OUTAGE_CALL_TIMEOUT_SECONDS
    + _PRIMARY_RECOVERY_WORST_CASE_SECONDS
    + DOCKER_STEP_BUDGET_SECONDS
)

# How many probes the failure message spells out. The whole point is that a red
# CI run tells us WHICH hypothesis it was — a budget that only bought 2 timing
# out probes, or a router that really kept announcing the dead primary — without
# printing the hundreds of probes a fast-refusing endpoint produces.
_PROBES_IN_FAILURE_MESSAGE = 5

_STOPPED_MARKER_PREFIX = "dse-chaos-stopped-containers"


def _gateway_base() -> str:
    return os.environ.get("DSE_MODEL_GATEWAY_BASE_URL", "http://localhost:4000")


def _master_key() -> str:
    return os.environ.get("DSE_LITELLM_MASTER_KEY", "sk-dse-local-dev-master-key")


def docker(*args: str) -> None:
    subprocess.run(["docker", *args], check=True, capture_output=True, timeout=60)


# --- Crash-safe record of "we stopped this container" ------------------------
# A test killed by the per-test timeout never runs its `finally`, so the only way
# the primary echo gets restarted is for a LATER process to know it was stopped.
# The record is best effort by design: a stale entry costs one redundant
# `docker start`, a missing one costs what we are trying to avoid.
#
# The directory is machine-wide (override with DSE_CHAOS_STATE_DIR) because the
# containers are, but the FILE is named after the pytest process that wrote it.
# That is the whole difference between repairing a dead run and sabotaging a live
# one: a shared file made the session-start repair `docker start` whatever was
# listed, including the container another still-running session had just stopped
# on purpose — mid-test, so that session's call would suddenly be served by the
# primary it had taken down.


def _state_dir() -> Path:
    return Path(os.environ.get("DSE_CHAOS_STATE_DIR", tempfile.gettempdir()))


def _marker_path(pid: int | None = None) -> Path:
    return _state_dir() / f"{_STOPPED_MARKER_PREFIX}.{os.getpid() if pid is None else pid}"


def _pid_alive(pid: int) -> bool:
    """Whether the process that wrote a marker is still around.

    Anything other than "this pid does not exist" counts as alive (a
    PermissionError means it exists and belongs to another user), because the
    consequence of guessing wrong in that direction is only a container left
    stopped, while guessing "dead" for a live session restarts a container that
    session is deliberately keeping down."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _recorded_stopped(pid: int | None = None) -> list[str]:
    try:
        text = _marker_path(pid).read_text()
    except OSError:
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


def _write_recorded_stopped(names: list[str], *, path: Path | None = None) -> None:
    path = _marker_path() if path is None else path
    try:
        if names:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(names) + "\n")
        else:
            path.unlink(missing_ok=True)
    except OSError:
        # Never fail a test over the safety net itself.
        pass


def stop_container(name: str) -> None:
    # Recorded BEFORE the stop: being killed between the two only costs a
    # redundant `docker start` next session, the other order costs a container
    # left stopped with nothing on disk saying so.
    recorded = _recorded_stopped()
    if name not in recorded:
        _write_recorded_stopped([*recorded, name])
    docker("stop", name)


def start_container(name: str) -> None:
    docker("start", name)
    _write_recorded_stopped([n for n in _recorded_stopped() if n != name])


def restore_containers_from_aborted_run() -> list[str]:
    """Restarts containers stopped by a pytest process that is NO LONGER RUNNING.

    Called once per session from conftest.py. This is the repair path for the
    `--timeout-method=thread` kill (os._exit(1) — no `finally`, no `atexit`):
    without it, one test blowing the 180s ceiling while the primary echo is
    stopped leaves the gateway degraded for every following run, and for the
    other agents sharing this Docker daemon.

    A marker whose process is still alive is skipped, including this session's
    own: `docker start` on a container that a running session stopped on purpose
    breaks THAT session's test, which is worse than leaving a container stopped.

    Returns the names it actually restarted (empty in the normal case, where no
    dead session's marker exists and no docker command is run at all).
    """
    restored: list[str] = []
    for marker in sorted(_state_dir().glob(f"{_STOPPED_MARKER_PREFIX}.*")):
        try:
            pid = int(marker.name.rsplit(".", 1)[-1])
        except ValueError:
            continue
        if _pid_alive(pid):
            continue
        pending = _recorded_stopped(pid)
        still_pending: list[str] = []
        for name in pending:
            try:
                # check=False: the container may already be running (a clean run
                # that raced the marker) — restoring has to be idempotent.
                completed = subprocess.run(
                    ["docker", "start", name], capture_output=True, timeout=60, check=False
                )
            except (OSError, subprocess.SubprocessError):
                # No docker on this machine, or the daemon is not answering: keep
                # the record (someone else will need it) and never break session
                # setup over the safety net itself.
                still_pending.append(name)
                continue
            if completed.returncode == 0:
                restored.append(name)
            else:
                still_pending.append(name)
        if pending != still_pending:
            _write_recorded_stopped(still_pending, path=marker)
    return restored


def raw_completion(
    content: str,
    *,
    model: str = ECHO_MODEL,
    key: str | None = None,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> httpx.Response:
    """Raw gateway call (bypassing the instrumented client) — used by the health
    helpers so they do not pollute the tests' ledger/audit."""
    return httpx.post(
        f"{_gateway_base()}/v1/chat/completions",
        headers={"Authorization": f"Bearer {key or _master_key()}"},
        json={"model": model, "messages": [{"role": "user", "content": content}]},
        timeout=timeout,
    )


@dataclass(frozen=True)
class _Probe:
    """One observation of "who is serving right now", kept so a failed wait can
    report the evidence instead of a single stale header value."""

    elapsed_seconds: float
    status_code: int | None
    api_base: str | None
    attempted_fallbacks: str | None
    error: str | None

    def __str__(self) -> str:
        if self.error is not None:
            return f"[{self.elapsed_seconds:.1f}s {self.error}]"
        return (
            f"[{self.elapsed_seconds:.1f}s status={self.status_code} "
            f"api_base={self.api_base!r} attempted_fallbacks={self.attempted_fallbacks!r}]"
        )


def _probe(content: str, timeout: float) -> _Probe:
    started = time.monotonic()
    try:
        resp = raw_completion(content, timeout=timeout)
    except httpx.HTTPError as exc:
        # A transport error is a legitimate observation (the proxy itself may be
        # restarting), not a reason to abort the wait — but it must be RECORDED,
        # because "every probe timed out" and "the router kept answering with the
        # dead primary" are different bugs that the old message could not tell
        # apart: it kept the api_base of the last probe that did NOT raise.
        return _Probe(time.monotonic() - started, None, None, None, type(exc).__name__)
    return _Probe(
        time.monotonic() - started,
        resp.status_code,
        resp.headers.get(MODEL_API_BASE_HEADER),
        resp.headers.get(ATTEMPTED_FALLBACKS_HEADER),
        None,
    )


def _wait_until_served_by(
    expected_api_base: str,
    *,
    content: str,
    failure: str,
    timeout_seconds: float,
    poll_interval: float,
    probe_timeout: float,
) -> None:
    started = time.monotonic()
    deadline = started + timeout_seconds
    probes: list[_Probe] = []
    while True:
        # Always evaluate the probe we started, even if it outlived the budget:
        # it still carries the answer we are waiting for, and throwing it away
        # was half of the old flakiness.
        probes.append(_probe(content, probe_timeout))
        if probes[-1].status_code == 200 and probes[-1].api_base == expected_api_base:
            return
        # `timeout_seconds` is the window during which probes are STARTED, which
        # is what the message below quotes. Refusing to start a probe that could
        # not FINISH inside it (an earlier attempt at keeping the wait's worst case
        # equal to its budget) left `budget - probe_timeout - poll_interval` of
        # real polling: 4s of a 30s budget for ensure_primary_serving, i.e. one
        # single probe, while the message still promised 30s — so a container that
        # needed a few more seconds to accept connections failed the wait, the very
        # flake this harness exists to remove. The in-flight tail is paid for in
        # WORST_CASE_*_SECONDS instead.
        if time.monotonic() + poll_interval >= deadline:
            break
        time.sleep(poll_interval)
    shown = probes[-_PROBES_IN_FAILURE_MESSAGE:]
    raise AssertionError(
        f"{failure} within its {timeout_seconds}s polling window "
        f"(gave up after {time.monotonic() - started:.1f}s, last probe included; "
        f"expected api_base={expected_api_base!r}; {len(probes)} probe(s) made, "
        f"last {len(shown)}: " + " ".join(str(p) for p in shown) + ")"
    )


def ensure_primary_serving(
    timeout_seconds: float = PRIMARY_RECOVERY_TIMEOUT_SECONDS,
    *,
    probe_timeout: float = PRIMARY_RECOVERY_PROBE_TIMEOUT_SECONDS,
) -> None:
    """Waits for the PRIMARY deployment to serve again (container up again).
    Fails loudly if it does not come back — leaving the gateway degraded would
    break the following tests and the other agents running in parallel."""
    _wait_until_served_by(
        PRIMARY_API_BASE,
        content="healthcheck-primary",
        failure="primary never came back to serving",
        timeout_seconds=timeout_seconds,
        poll_interval=PRIMARY_RECOVERY_POLL_INTERVAL_SECONDS,
        probe_timeout=probe_timeout,
    )


def wait_until_fallback_serves(
    timeout_seconds: float = FALLBACK_TAKEOVER_TIMEOUT_SECONDS,
    *,
    probe_timeout: float = PROBE_TIMEOUT_SECONDS,
) -> None:
    """After taking the primary down, waits for the router to STABILIZE serving
    from the FALLBACK (echo-b) — this removes the race between `docker stop` and
    the test's instrumented call (the primary could still be serving, so there
    was no degradation at all). Uses raw_completion (master key directly), which
    does NOT write to the tests' ledger/audit.

    The normal outcome is a RUNTIME fallback on this very probe
    (attempted_fallbacks>=1): with the proxy's `allowed_fails`/`cooldown_time`
    pair the primary is not taken out of the pool, on purpose (see the note in
    litellm_config.yaml). If a cooldown does land, the request goes STRAIGHT to
    the fallback with attempted_fallbacks=0 — also fine for the tests that
    follow, both are auditable degradation."""
    _wait_until_served_by(
        FALLBACK_API_BASE,
        content="healthcheck-fallback",
        failure="fallback never took over",
        timeout_seconds=timeout_seconds,
        poll_interval=FALLBACK_TAKEOVER_POLL_INTERVAL_SECONDS,
        probe_timeout=probe_timeout,
    )
