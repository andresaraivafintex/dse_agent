"""Shared fixtures for the WS-D tests.

These tests run against REAL infra (Postgres on localhost:5432, dev Vault on
localhost:8200, and the LiteLLM proxy + echo model brought up via
`docker-compose.wsd.yml`, see README "Como rodar os testes"). Nothing here is
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
