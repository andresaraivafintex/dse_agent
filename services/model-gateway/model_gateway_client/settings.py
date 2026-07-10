"""Configuração do `model_gateway_client` — tudo por env var, com um único
ponto de leitura opcional no Vault dev da fundação para o master key do
LiteLLM (WSD-E1-T3).

Por que Vault é "opcional com fallback" e não obrigatório: `services/platform/`
(WS-F) ainda não publicou um client de leitura de Vault compartilhado no
momento em que este código foi escrito (workstreams rodam em paralelo). Este
módulo já fala com o Vault dev real da fundação (`localhost:8200`, KV v2 em
`secret/`) via HTTP puro — não é fixture — mas se Vault não estiver acessível
(ou o secret não existir) cai para a env var local `DSE_LITELLM_MASTER_KEY`,
documentado explicitamente como temporário. Ver README.md.
"""
from __future__ import annotations

import os

DEFAULT_GATEWAY_BASE_URL = "http://localhost:4000"
DEFAULT_MASTER_KEY = "sk-dse-local-dev-master-key"
DEFAULT_VAULT_ADDR = "http://localhost:8200"
DEFAULT_VAULT_SECRET_PATH = "secret/data/model-gateway/master-key"


def gateway_base_url() -> str:
    """Base URL única do model-gateway (dse_contracts.gateway_contract). Todo
    consumo de modelo passa por aqui — nunca um SDK de provider direto."""
    return os.environ.get("DSE_MODEL_GATEWAY_BASE_URL", DEFAULT_GATEWAY_BASE_URL)


def litellm_admin_master_key() -> str:
    """Master key admin do LiteLLM — usada SÓ para /key/generate e /key/delete
    (nunca para chamadas de modelo em si, que usam a virtual key emitida).

    Ordem de resolução:
      1. Vault dev real (`VAULT_ADDR`/`VAULT_TOKEN`, path configurável via
         `DSE_LITELLM_MASTER_KEY_VAULT_PATH`) — caminho de produção.
      2. Env var `DSE_LITELLM_MASTER_KEY` — fallback local/dev, TEMPORÁRIO
         (ver README: "o que falta para produção").
      3. Default hardcoded de dev (mesmo valor usado no docker-compose.wsd.yml)
         — só para não quebrar `pytest` em uma máquina sem nada configurado.
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
        # Vault indisponível não deve quebrar o processo — cai para o
        # fallback de env var (ver docstring acima).
        return None


def virtual_keys_database_url() -> str:
    """Postgres do control-plane DSE (schema_migrations 0005_wsd.sql,
    tabela `virtual_keys`) — reaproveita a mesma convenção de
    `DSE_DATABASE_URL` usada no resto do monorepo (ver CONVENTIONS.md)."""
    return os.environ.get(
        "DSE_DATABASE_URL", "postgresql://dse:dse_dev_only@localhost:5432/dse"
    )


def otlp_exporter_endpoint() -> str | None:
    """Endpoint do OTel collector do WS-F (produção). Sem valor -> spans só
    ficam no recorder em memória (usado por testes e pelo cost_export local)."""
    return os.environ.get("DSE_OTEL_EXPORTER_OTLP_ENDPOINT")
