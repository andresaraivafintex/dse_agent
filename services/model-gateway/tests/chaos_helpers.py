"""Shared helpers for WS-D's chaos/failover tests (Phase 3).

REAL chaos: we take down WS-D's own containers (`docker stop`) — never the
foundation's shared infra (Postgres/Temporal/Redis/Vault) nor other
workstreams' services. Any test that takes an echo down MUST restore it in
`finally` and wait for the primary to serve again (`ensure_primary_serving`) so
it does not leak degraded state to other tests/agents running in parallel.
"""
from __future__ import annotations

import os
import subprocess
import time

import httpx

PRIMARY_ECHO_CONTAINER = "dse_model_gateway_echo"
FALLBACK_ECHO_CONTAINER = "dse_model_gateway_echo_b"
PRIMARY_API_BASE = "http://model-gateway-echo:9000"
FALLBACK_API_BASE = "http://model-gateway-echo-b:9000"

ECHO_MODEL = "eco/echo-model"
ECHO_MODEL_B = "eco/echo-model-b"


def _gateway_base() -> str:
    return os.environ.get("DSE_MODEL_GATEWAY_BASE_URL", "http://localhost:4000")


def _master_key() -> str:
    return os.environ.get("DSE_LITELLM_MASTER_KEY", "sk-dse-local-dev-master-key")


def docker(*args: str) -> None:
    subprocess.run(["docker", *args], check=True, capture_output=True, timeout=60)


def stop_container(name: str) -> None:
    docker("stop", name)


def start_container(name: str) -> None:
    docker("start", name)


def raw_completion(content: str, *, model: str = ECHO_MODEL, key: str | None = None) -> httpx.Response:
    """Raw gateway call (bypassing the instrumented client) — used by the health
    helpers so they do not pollute the tests' ledger/audit."""
    return httpx.post(
        f"{_gateway_base()}/v1/chat/completions",
        headers={"Authorization": f"Bearer {key or _master_key()}"},
        json={"model": model, "messages": [{"role": "user", "content": content}]},
        timeout=15.0,
    )


def ensure_primary_serving(timeout_seconds: float = 60.0) -> None:
    """Waits for the PRIMARY deployment to serve again (container up + out of
    the router's cooldown). Fails loudly if it does not come back — leaving the
    gateway degraded would break the following tests and the other agents
    running in parallel."""
    deadline = time.monotonic() + timeout_seconds
    last: str | None = None
    while time.monotonic() < deadline:
        try:
            resp = raw_completion("healthcheck-primary")
            last = resp.headers.get("x-litellm-model-api-base")
            if resp.status_code == 200 and last == PRIMARY_API_BASE:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1.0)
    raise AssertionError(
        f"primary never came back to serving within {timeout_seconds}s (last api_base={last!r})"
    )


def wait_until_fallback_serves(timeout_seconds: float = 60.0) -> None:
    """After taking the primary down, waits for the router to STABILIZE serving
    from the FALLBACK (echo-b) — this removes the race between `docker stop` and
    the test's instrumented call (the primary could still be serving, so there
    was no degradation at all). Uses raw_completion (master key directly), which
    does NOT write to the tests' ledger/audit. After this the primary is in
    cooldown, so the instrumented call goes STRAIGHT to the fallback
    (attempted_fallbacks=0) — cooldown degradation, detected via
    DSE_FALLBACK_API_BASES."""
    deadline = time.monotonic() + timeout_seconds
    last: str | None = None
    while time.monotonic() < deadline:
        try:
            resp = raw_completion("healthcheck-fallback")
            last = resp.headers.get("x-litellm-model-api-base")
            if resp.status_code == 200 and last == FALLBACK_API_BASE:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise AssertionError(
        f"fallback never took over within {timeout_seconds}s (last api_base={last!r})"
    )
