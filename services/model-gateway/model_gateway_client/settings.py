"""`model_gateway_client` configuration — everything via env var, with a
single optional read from the foundation's dev Vault for the LiteLLM master
key (WSD-E1-T3).

Why Vault is "optional with fallback" and not mandatory: `services/platform/`
(WS-F) had not yet published a shared Vault read client when this code was
written (workstreams run in parallel). This module already talks to the
foundation's real dev Vault (`localhost:8200`, KV v2 under `secret/`) over
plain HTTP — it is not a fixture — but if Vault is unreachable (or the secret
does not exist) it falls back to the local `DSE_LITELLM_MASTER_KEY` env var,
explicitly documented as temporary. See README.md.
"""
from __future__ import annotations

import os

DEFAULT_GATEWAY_BASE_URL = "http://localhost:4000"
DEFAULT_MASTER_KEY = "sk-dse-local-dev-master-key"
DEFAULT_VAULT_ADDR = "http://localhost:8200"
DEFAULT_VAULT_SECRET_PATH = "secret/data/model-gateway/master-key"


def gateway_base_url() -> str:
    """The single model-gateway base URL (dse_contracts.gateway_contract). All
    model consumption goes through here — never a direct provider SDK."""
    return os.environ.get("DSE_MODEL_GATEWAY_BASE_URL", DEFAULT_GATEWAY_BASE_URL)


def litellm_admin_master_key() -> str:
    """LiteLLM admin master key — used ONLY for /key/generate and /key/delete
    (never for model calls themselves, which use the issued virtual key).

    Resolution order:
      1. Real dev Vault (`VAULT_ADDR`/`VAULT_TOKEN`, path configurable via
         `DSE_LITELLM_MASTER_KEY_VAULT_PATH`) — the production path.
      2. `DSE_LITELLM_MASTER_KEY` env var — local/dev fallback, TEMPORARY
         (see README: "what is still missing for production").
      3. Hardcoded dev default (same value used in docker-compose.wsd.yml) —
         only so `pytest` does not break on a machine with nothing configured.
    """
    from_vault = _read_master_key_from_vault()
    if from_vault:
        return from_vault
    return os.environ.get("DSE_LITELLM_MASTER_KEY", DEFAULT_MASTER_KEY)


def _read_master_key_from_vault() -> str | None:
    addr = os.environ.get("VAULT_ADDR")
    token = os.environ.get("VAULT_TOKEN")
    if not addr or not token:
        return None
    path = os.environ.get("DSE_LITELLM_MASTER_KEY_VAULT_PATH", DEFAULT_VAULT_SECRET_PATH)
    try:
        import httpx

        resp = httpx.get(
            f"{addr}/v1/{path}",
            headers={"X-Vault-Token": token},
            timeout=3.0,
        )
        if resp.status_code != 200:
            return None
        payload = resp.json()
        return payload.get("data", {}).get("data", {}).get("value")
    except Exception:
        # An unavailable Vault must not break the process — fall back to the
        # env var (see docstring above).
        return None


def virtual_keys_database_url() -> str:
    """DSE control-plane Postgres (schema_migrations 0005_wsd.sql,
    `virtual_keys` table) — reuses the same `DSE_DATABASE_URL` convention used
    across the rest of the monorepo (see CONVENTIONS.md)."""
    return os.environ.get(
        "DSE_DATABASE_URL", "postgresql://dse:dse_dev_only@localhost:5432/dse"
    )


def otlp_exporter_endpoint() -> str | None:
    """WS-F OTel collector endpoint (production). Unset -> spans stay only in
    the in-memory recorder (used by tests and by the local cost_export)."""
    return os.environ.get("DSE_OTEL_EXPORTER_OTLP_ENDPOINT")
