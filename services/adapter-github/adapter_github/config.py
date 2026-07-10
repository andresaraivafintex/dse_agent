"""Configuração do adapter GitHub — leitura de credenciais da GitHub App.

Mesma convenção do adapter-slack (WSA-E2-T1): backend de secrets do WS-F
(`dse_secrets`, WSF-E2-T3a) PRIMEIRO se disponível/responder; senão cai
para env var local. Nenhuma GitHub App real foi registrada nesta sessão —
a leitura do Vault sempre cai (via `VaultUnavailableError`) para as env
vars abaixo, que devem ser preenchidas com valores de fixture para
rodar os testes locais (`FakeGithubClient` cobre o outbound sem precisar
de nenhuma delas)."""
from __future__ import annotations

import os


def get_tenant_id() -> str:
    """Fase 1: single-tenant de desenvolvimento — ver mesma nota em
    `adapter_slack.config.get_tenant_id` (mapear org/repo GitHub -> tenant é
    escopo de WS-F/Fase 2)."""
    return os.environ.get("DSE_TENANT_ID", "tenant_dev")


def _from_vault_or_env(vault_path: str, vault_key: str, env_var: str) -> str:
    try:
        from dse_secrets import VaultUnavailableError, get_secret

        try:
            return get_secret(vault_path)[vault_key]
        except VaultUnavailableError:
            pass
    except ImportError:
        pass
    return os.environ.get(env_var, "")


def get_webhook_secret() -> str:
    return _from_vault_or_env("dse/github/app", "webhook_secret", "GITHUB_WEBHOOK_SECRET")


def get_app_id() -> str:
    return _from_vault_or_env("dse/github/app", "app_id", "GITHUB_APP_ID")


def get_app_private_key() -> str:
    return _from_vault_or_env("dse/github/app", "private_key", "GITHUB_APP_PRIVATE_KEY")


def get_installation_id() -> str:
    return _from_vault_or_env("dse/github/app", "installation_id", "GITHUB_APP_INSTALLATION_ID")


def get_bot_mention_login() -> str:
    """Login (sem @) que o adapter reconhece como 'menção ao bot' em
    comentários de issue comuns (não-PR). Fixture de dev: `dse-bot`."""
    return os.environ.get("GITHUB_BOT_LOGIN", "dse-bot")


def get_task_label() -> str:
    return os.environ.get("GITHUB_TASK_LABEL", "dse")
