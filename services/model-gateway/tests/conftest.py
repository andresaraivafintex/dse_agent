"""Shared fixtures for the WS-D tests.

These tests run against REAL infra (Postgres on localhost:5432, dev Vault on
localhost:8200, and the LiteLLM proxy + echo model brought up via
`docker-compose.wsd.yml`, see README "How to run the tests"). Nothing here is
mocked — the guarantee we are proving (a virtual key actually issued, actually
revoked, an audit row actually written) is the whole point of the system (P8).
"""
from __future__ import annotations

import os
import uuid

import pytest

# Defaults matching the foundation's docker-compose.wsd.yml /
# docker-compose.yml — only applied when the env var is not already set, so
# anyone running against other infra (CI, another machine) can override them.
os.environ.setdefault("DSE_MODEL_GATEWAY_BASE_URL", "http://localhost:4000")
os.environ.setdefault("DSE_LITELLM_MASTER_KEY", "sk-dse-local-dev-master-key")
os.environ.setdefault("VAULT_ADDR", "http://localhost:8200")
os.environ.setdefault("VAULT_TOKEN", "dse_dev_root")
os.environ.setdefault("DSE_DATABASE_URL", "postgresql://dse:dse_dev_only@localhost:5432/dse")
os.environ.setdefault(
    "DSE_AUDIT_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
)

ECHO_MODEL = "eco/echo-model"


@pytest.fixture(scope="session", autouse=True)
def _repair_containers_left_stopped_by_an_aborted_run():
    """Restarts an echo container stopped by a pytest process that is no longer
    running.

    The chaos tests restore their containers in `finally`, which covers an
    assertion failure but NOT the suite's own per-test ceiling: scripts/
    test_matrix.py runs pytest with `--timeout=180 --timeout-method=thread`, and
    that method dumps the stacks and calls `os._exit(1)` — no `finally`, no
    `atexit`. One test blowing the ceiling while the primary echo is stopped used
    to leave the gateway degraded for every later run on this machine. The stop is
    recorded on disk (chaos_helpers), so the repair happens here, at the start of
    the next session.

    It only ever repairs after a process that is GONE: the record is a file named
    after the pytest process that wrote it, and one belonging to a live pid is
    skipped. Restarting a container that a still-running session deliberately
    stopped would make that session's failover assertions a coin flip, which is
    worse than leaving a container stopped. No docker command runs when no dead
    session's record exists."""
    from .chaos_helpers import restore_containers_from_aborted_run

    restored = restore_containers_from_aborted_run()
    if restored:
        print(
            "[chaos] restarted container(s) left stopped by an aborted run: "
            + ", ".join(restored)
        )
    yield


@pytest.fixture
def unique_ids():
    """Unique IDs per test — the tests run against real, shared
    Postgres/LiteLLM, so never reusing fixed tenant/work_item values keeps one
    test from seeing another's leftovers."""
    suffix = uuid.uuid4().hex[:10]
    return {
        "tenant_id": f"tenant-test-{suffix}",
        "work_item_id": f"wi-test-{suffix}",
    }


@pytest.fixture(autouse=True)
def _clear_span_recorder():
    """Every test starts with a clean in-memory span recorder (avoids
    cross-contamination between telemetry/cost_export tests, which read the
    process's whole buffer)."""
    from model_gateway_client import telemetry

    telemetry.clear_recorded_spans()
    yield
    telemetry.clear_recorded_spans()
