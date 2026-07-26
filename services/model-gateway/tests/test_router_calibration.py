"""Calibration invariants for the proxy's failover config and the chaos harness.

Two distinct failures motivate this file.

1. test_failover_intra_tier.py was intermittently red on main with
   "fallback never took over within 60.0s (last api_base='http://model-gateway-echo:9000')".
   The cause was the HARNESS: the probe's client timeout (15s) was shorter than
   what the proxy spends answering one request with the primary echo stopped
   (2 attempts x `timeout: 5` plus the router's retry back-off), and the old poll
   loop swallowed the resulting ReadTimeout while keeping the api_base of an
   earlier probe — which is why the message named the primary and read like a
   router bug. Each wait's probe timeout is now derived from the config
   (test_the_takeover_probe_outlives_the_dead_primary_group,
   test_the_recovery_probe_outlives_one_attempt_against_the_primary) and every
   window is checked to afford more than one probe
   (test_each_wait_budget_can_afford_more_than_one_probe) — a budget that buys a
   single probe is the same flake in a new place.

2. The first attempt to fix (1) recalibrated the ROUTER instead
   (`allowed_fails: 0` + `cooldown_time: 30`) and would have taken the
   no-fallback production model groups out of service for 30s on a single
   timeout. That is what
   test_no_model_group_can_be_taken_out_of_service_by_a_single_failure exists to
   stop from coming back.

These tests are declarative: they parse the proxy's real config, the chaos
helpers' real constants and the CI runner's real per-test ceiling. No Docker, no
Postgres, no proxy.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

from . import chaos_helpers

_SERVICE_ROOT = Path(__file__).resolve().parent.parent
_LITELLM_CONFIG = _SERVICE_ROOT / "litellm_config.yaml"
_TEST_MATRIX = _SERVICE_ROOT.parent.parent / "scripts" / "test_matrix.py"
_SUITE = "services/model-gateway"

# The two deployments the chaos battery stops and restarts.
_CHAOS_DEPLOYMENTS = ("eco/echo-model", "eco/echo-model-b")

# LiteLLM's back-off between the attempts of one model group:
# INITIAL_RETRY_DELAY (0.5) * 2**0 + JITTER (<=0.75) — litellm/constants.py +
# litellm/utils.py::_calculate_retry_after. Counted once per group.
_RETRY_BACKOFF_ALLOWANCE_SECONDS = 1.25


@pytest.fixture(scope="module")
def config() -> dict:
    return yaml.safe_load(_LITELLM_CONFIG.read_text())


def _router_settings(config: dict) -> dict:
    return config.get("router_settings") or {}


def _fallback_map(config: dict) -> dict[str, list[str]]:
    routes: dict[str, list[str]] = {}
    for entry in _router_settings(config).get("fallbacks", []) or []:
        for primary, targets in entry.items():
            routes[primary] = list(targets)
    return routes


def _deployment(config: dict, model_name: str) -> dict:
    for entry in config["model_list"]:
        if entry["model_name"] == model_name:
            return entry
    raise AssertionError(f"{model_name} is not registered in model_list")


def _effective_cooldown_seconds(config: dict, model_name: str) -> float:
    """The window LiteLLM actually applies: the deployment's own `cooldown_time`
    wins over the router default (Router.deployment_callback_on_failure resolves
    deployment config > `retry-after` header > router default)."""
    params = _deployment(config, model_name)["litellm_params"]
    if params.get("cooldown_time") is not None:
        return float(params["cooldown_time"])
    return float(_router_settings(config)["cooldown_time"])


def _per_attempt_timeout_seconds(config: dict, model_name: str) -> float:
    """The cap LiteLLM applies to ONE attempt against this deployment: its own
    `timeout`, or the global `request_timeout` when it declares none
    (litellm_core_utils/completion_timeout.py)."""
    params = _deployment(config, model_name)["litellm_params"]
    return float(
        params.get("timeout") or (config.get("litellm_settings") or {})["request_timeout"]
    )


def _dead_group_cost_seconds(config: dict, model_name: str) -> float:
    """Wall time the proxy spends on ONE model group whose endpoint is DOWN before
    it moves on to the next group: every attempt hangs until its per-attempt
    timeout, `num_retries` buys one more, plus one retry back-off."""
    attempts = 1 + int(_router_settings(config).get("num_retries", 0))
    return attempts * _per_attempt_timeout_seconds(config, model_name) + (
        _RETRY_BACKOFF_ALLOWANCE_SECONDS
    )


def _ci_per_test_timeout_seconds() -> float:
    """The ceiling CI really enforces for this suite, read out of
    scripts/test_matrix.py (`--timeout=N --timeout-method=thread`). Parsed rather
    than hardcoded because the whole point of the budget arithmetic below is that
    it cannot drift away from the real number."""
    module = ast.parse(_TEST_MATRIX.read_text())
    default: float | None = None
    per_suite: dict[str, int] = {}
    for node in module.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
            value = node.value
        else:
            continue
        if value is None:
            continue
        if "DEFAULT_TEST_TIMEOUT_S" in targets:
            default = float(ast.literal_eval(value))
        elif "SUITE_TIMEOUTS" in targets:
            per_suite = ast.literal_eval(value)
    assert default is not None, (
        f"could not read DEFAULT_TEST_TIMEOUT_S from {_TEST_MATRIX} — the per-test "
        "ceiling moved or was renamed, so the chaos budgets below have to be re-checked"
    )
    return float(per_suite.get(_SUITE, default))


def test_no_model_group_can_be_taken_out_of_service_by_a_single_failure(config: dict):
    """`allowed_fails` is ROUTER-WIDE, and most of our model groups have exactly
    one deployment and no fallback route (`anthropic/claude*`, `bedrock/*`).

    With `allowed_fails: 0` the FIRST coolable failure takes that deployment out
    of the pool, i.e. the whole group, and the proxy answers "No deployments
    available" as HTTP 429 to every tenant for the cooldown window
    (litellm/proxy/_types.py maps that message to 429). A plain timeout is
    coolable — `_is_cooldown_required` returns True for 408 — and
    `litellm_settings.request_timeout` is the PER-ATTEMPT cap for those groups
    (they declare no `timeout` of their own), so one long generation crossing it
    would be enough. That is a self-inflicted outage, and it was briefly
    configured here to make a chaos test deterministic; the harness was fixed
    instead."""
    groups_without_fallback = sorted(
        entry["model_name"]
        for entry in config["model_list"]
        if entry["model_name"] not in _fallback_map(config)
    )
    assert groups_without_fallback, (
        "expected model groups with no fallback route (the production tiers) — "
        "if that changed, re-read the reasoning in this test before relaxing it"
    )
    assert _router_settings(config).get("allowed_fails") != 0, (
        "allowed_fails: 0 cools a deployment down on the first failure; these "
        f"groups have nowhere to fall back to and would return 429 to every "
        f"tenant for the whole window: {groups_without_fallback}"
    )


def test_the_allowed_fails_policy_declares_no_inert_error_classes(config: dict):
    """`BadRequestErrorAllowedFails` and `ContentPolicyViolationErrorAllowedFails`
    look like they protect a deployment from being cooled down over a caller's bad
    prompt. They cannot: `_is_cooldown_required` (LiteLLM 1.93.0) returns True in
    the 4xx range ONLY for 429/401/408/404, so a 400 — which is what
    ContentPolicyViolationError is — never reaches the allowed-fails policy at
    all. Configuring them buys nothing and documents a guarantee we do not have."""
    policy = _router_settings(config).get("allowed_fails_policy") or {}
    for key in ("BadRequestErrorAllowedFails", "ContentPolicyViolationErrorAllowedFails"):
        assert key not in policy, (
            f"{key} never reaches LiteLLM's allowed-fails policy (400s are not "
            "coolable) — remove it instead of implying it does something"
        )


def test_the_takeover_probe_outlives_the_dead_primary_group(config: dict):
    """`wait_until_fallback_serves` probes while the primary echo is stopped, so
    its answer only arrives after the proxy has burnt the dead primary group. A
    probe cut off before that reports nothing at all (not even which endpoint
    served) and still spends its share of the wait window — that is how a 60s
    budget used to be burnt by four 15s ReadTimeouts while the failure message
    quoted an api_base from a probe minutes earlier."""
    dead_group = _dead_group_cost_seconds(config, chaos_helpers.ECHO_MODEL)
    assert chaos_helpers.PROBE_TIMEOUT_SECONDS >= dead_group, (
        f"a probe gives up after {chaos_helpers.PROBE_TIMEOUT_SECONDS}s but the proxy "
        f"spends {dead_group}s on the stopped {chaos_helpers.ECHO_MODEL} group before "
        "the fallback group answers"
    )


def test_the_recovery_probe_outlives_one_attempt_against_the_primary(config: dict):
    """`ensure_primary_serving` waits for a HEALTHY primary, which is the first
    deployment tried and answers in milliseconds; its probes are deliberately much
    cheaper than the takeover ones so the wait can poll several times inside its
    window. The floor is the deployment's own per-attempt cap: below it, a probe
    could be cut off while the proxy was still allowed to be working on the answer,
    and the wait would report a recovery that had already happened as a timeout."""
    per_attempt = _per_attempt_timeout_seconds(config, chaos_helpers.ECHO_MODEL)
    assert chaos_helpers.PRIMARY_RECOVERY_PROBE_TIMEOUT_SECONDS >= per_attempt, (
        f"a recovery probe gives up after "
        f"{chaos_helpers.PRIMARY_RECOVERY_PROBE_TIMEOUT_SECONDS}s, less than the "
        f"{per_attempt}s LiteLLM allows ONE attempt against {chaos_helpers.ECHO_MODEL}"
    )


def test_the_total_outage_ceiling_follows_the_measurement_not_the_model(config: dict):
    """The per-group model above is a LOWER bound once EVERY group is dead: it
    predicts 2 x `_dead_group_cost_seconds` = 22.5s for both echoes stopped, and
    this repository measured ~53s for exactly that state (the unmodelled time is in
    LiteLLM's give-up path, after the last fallback also fails). So the ceiling for
    the total-outage call is sized from the measurement, and the model is not used
    to justify it. A ceiling below the measured cost would turn that test's
    deliberate outage into a client-side timeout instead of the typed refusal it
    asserts."""
    both_groups_modelled = 2 * _dead_group_cost_seconds(config, chaos_helpers.ECHO_MODEL)
    measured = chaos_helpers.MEASURED_BOTH_ECHOES_DOWN_ANSWER_SECONDS
    assert measured > both_groups_modelled, (
        f"the per-group model predicts {both_groups_modelled}s for both echoes down "
        f"and the recorded measurement is {measured}s: the model no longer UNDER-"
        "predicts that path, so one of the two changed — re-measure before sizing "
        "anything from either"
    )
    assert chaos_helpers.TOTAL_OUTAGE_CALL_TIMEOUT_SECONDS >= measured, (
        f"the total-outage call gives up after "
        f"{chaos_helpers.TOTAL_OUTAGE_CALL_TIMEOUT_SECONDS}s, below the {measured}s "
        "the proxy was measured to spend before it refuses"
    )


def test_each_wait_budget_can_afford_more_than_one_probe():
    """A wait is a POLL loop: after a probe that burns its whole timeout there has
    to be room, inside the window, for the poll interval and another probe —
    otherwise the window buys exactly one observation and the loop is decoration.
    Not a theoretical worry: with a 25s probe timeout, a 30s window and the guard
    that refused to start a probe it could not finish, the recovery wait really did
    make one single probe, and the message still claimed it had waited 30s. The
    old version of this test asserted `budget >= probe_timeout`, which the whole
    calibration file passed with exactly that property broken."""
    for name, probe_timeout, poll_interval in (
        (
            "PRIMARY_RECOVERY_TIMEOUT_SECONDS",
            chaos_helpers.PRIMARY_RECOVERY_PROBE_TIMEOUT_SECONDS,
            chaos_helpers.PRIMARY_RECOVERY_POLL_INTERVAL_SECONDS,
        ),
        (
            "FALLBACK_TAKEOVER_TIMEOUT_SECONDS",
            chaos_helpers.PROBE_TIMEOUT_SECONDS,
            chaos_helpers.FALLBACK_TAKEOVER_POLL_INTERVAL_SECONDS,
        ),
    ):
        budget = getattr(chaos_helpers, name)
        assert budget > probe_timeout + poll_interval, (
            f"{name}={budget}s cannot start a second probe after a {probe_timeout}s "
            f"one plus the {poll_interval}s poll interval — the window buys a single "
            "probe, not a poll loop"
        )


def test_the_chaos_tests_fit_inside_the_ci_per_test_ceiling():
    """The ceiling is enforced with `--timeout-method=thread`, which does NOT
    raise inside the test: pytest-timeout dumps the stacks and calls os._exit(1),
    skipping the `finally: start_container(...)`. Crossing it therefore leaves the
    primary echo stopped for every following run, so the waits and call timeouts
    of one test have to add up to less than the ceiling with room for the DB work
    around them."""
    ceiling = _ci_per_test_timeout_seconds()
    for name in ("WORST_CASE_SINGLE_ECHO_DOWN_SECONDS", "WORST_CASE_BOTH_ECHOES_DOWN_SECONDS"):
        worst_case = getattr(chaos_helpers, name)
        # 0.9: the remainder pays for minting/revoking a virtual key and the
        # audit/ledger queries the same tests make.
        assert worst_case <= 0.9 * ceiling, (
            f"{name}={worst_case}s leaves no room under the {ceiling}s per-test "
            "ceiling — a test that reaches it is KILLED, not failed, and the echo "
            "container stays stopped"
        )


def test_a_cooldown_cannot_outlast_the_recovery_budget(config: dict):
    """A cooled-down deployment is out of the pool for the whole window, so
    `ensure_primary_serving` cannot confirm the recovery before it expires. If a
    window ever grew past that budget, the RESTORE step would start failing and
    hand the next test a degraded gateway."""
    for model_name in _CHAOS_DEPLOYMENTS:
        cooldown = _effective_cooldown_seconds(config, model_name)
        assert cooldown < chaos_helpers.PRIMARY_RECOVERY_TIMEOUT_SECONDS, (
            f"{model_name}: cooldown_time={cooldown}s outlasts the "
            f"{chaos_helpers.PRIMARY_RECOVERY_TIMEOUT_SECONDS}s recovery budget"
        )
