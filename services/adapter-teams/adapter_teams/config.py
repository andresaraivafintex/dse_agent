"""Configuração do adapter Teams — credenciais e parâmetros.

Mesma convenção dos outros adapters WS-A (WSA-E2-T1): backend de secrets do
WS-F (`dse_secrets`) PRIMEIRO se disponível/responder, senão cai para env var
local. Sem tenant Teams real nesta sessão, o path do Vault (`dse/teams/webhook`,
`dse/teams/bot`) não tem versão gravada e a leitura cai (via
`VaultUnavailableError`) para as env vars abaixo — preenchidas com valores de
teste local para rodar as fixtures.

Dois modos de credencial (o adapter suporta ambos os transportes de inbound):
  - Outgoing webhook: `TEAMS_OUTGOING_SECRET` (Base64) — HMAC do corpo.
  - Bot Framework (Azure Bot): `TEAMS_APP_ID` + `TEAMS_APP_PASSWORD` — usados
    para obter o bearer token do connector REST (outbound) e, na ativação, para
    validar o JWT Bearer do inbound.
"""
from __future__ import annotations

import os


def _from_vault_or_env(vault_path: str, vault_key: str, env_var: str, default: str = "") -> str:
    try:
        from dse_secrets import VaultUnavailableError, get_secret

        try:
            return get_secret(vault_path)[vault_key]
        except VaultUnavailableError:
            pass
    except ImportError:
        pass
    return os.environ.get(env_var, default)


def get_tenant_id() -> str:
    """Fallback single-tenant (mesma env var dos outros adapters). A resolução
    real por AAD tenant guid -> tenant DSE é feita por
    `ingest_gateway.resolve_tenant` (WSA-E1-T5) na ativação."""
    return os.environ.get("DSE_TENANT_ID", "tenant_dev")


def get_teams_shared_secret() -> str:
    """Secret Base64 do outgoing webhook (usado pelo HMAC de assinatura)."""
    return _from_vault_or_env("dse/teams/webhook", "shared_secret", "TEAMS_OUTGOING_SECRET")


def get_teams_app_id() -> str:
    return _from_vault_or_env("dse/teams/bot", "app_id", "TEAMS_APP_ID")


def get_teams_app_password() -> str:
    return _from_vault_or_env("dse/teams/bot", "app_password", "TEAMS_APP_PASSWORD")


def get_default_service_url() -> str:
    """`serviceUrl` do Bot Framework connector (por região; ex.:
    `https://smba.trafficmanager.net/emea/`). Em produção vem em cada Activity
    recebida; este default é só para outbound iniciado internamente."""
    return _from_vault_or_env("dse/teams/bot", "service_url", "TEAMS_SERVICE_URL")
