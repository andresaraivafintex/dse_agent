"""GitHub adapter configuration — reading the GitHub App credentials.

Same convention as adapter-slack (WSA-E2-T1): the WS-F secrets backend
(`dse_secrets`, WSF-E2-T3a) FIRST if available/responsive; otherwise fall
back to a local env var. No real GitHub App was registered in this session —
the Vault read always falls back (via `VaultUnavailableError`) to the env
vars below, which must be filled with fixture values to run the local tests
(`FakeGithubClient` covers the outbound without needing any of them)."""
from __future__ import annotations

import os


def get_tenant_id() -> str:
    """Phase 1: development single-tenant — see the same note in
    `adapter_slack.config.get_tenant_id` (mapping GitHub org/repo -> tenant is
    WS-F/Phase 2 scope)."""
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
    """Login (without the @) that the adapter recognizes as a 'bot mention' in
    plain (non-PR) issue comments. Dev fixture: `dse-bot`."""
    return os.environ.get("GITHUB_BOT_LOGIN", "dse-bot")


def get_task_label() -> str:
    return os.environ.get("GITHUB_TASK_LABEL", "dse")
