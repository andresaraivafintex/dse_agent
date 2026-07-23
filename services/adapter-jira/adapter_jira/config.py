"""Configuração do adapter Jira — credenciais da service account + parâmetros
de trigger/aprovação. Mesma convenção dos outros adapters WS-A (WSA-E2-T1):
backend de secrets do WS-F (`dse_secrets`) PRIMEIRO se disponível/responder,
senão cai para env var local.

Sem um site Jira real registrado nesta sessão, a leitura do Vault sempre cai
(via `VaultUnavailableError`) para as env vars abaixo. O token da service
account é escopado (project-level) e trocado pelo Vault em produção
(`dse/jira/service_account`).
"""
from __future__ import annotations

import os


def get_tenant_id() -> str:
    """Fallback single-tenant (mesma env var dos outros adapters). A
    resolução real por site Jira -> tenant é feita por
    `ingest_gateway.resolve_tenant` (WSA-E1-T5); este é só o default."""
    return os.environ.get("DSE_TENANT_ID", "tenant_dev")


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


def get_webhook_secret() -> str:
    return _from_vault_or_env("dse/jira/service_account", "webhook_secret", "JIRA_WEBHOOK_SECRET")


def get_base_url() -> str:
    """URL do site Jira Cloud, ex.: `https://acme.atlassian.net`. Dobra como
    a `binding_key` de resolução de tenant (site -> tenant)."""
    return _from_vault_or_env("dse/jira/service_account", "base_url", "JIRA_BASE_URL")


def get_service_account_email() -> str:
    return _from_vault_or_env("dse/jira/service_account", "email", "JIRA_ACCOUNT_EMAIL")


def get_api_token() -> str:
    return _from_vault_or_env("dse/jira/service_account", "api_token", "JIRA_API_TOKEN")


def get_trigger_label() -> str:
    """Label que marca um issue como tarefa para o DSE (default `dse`)."""
    return os.environ.get("JIRA_TRIGGER_LABEL", "dse")


def get_plan_approved_status() -> str:
    """Nome da coluna/status cuja TRANSIÇÃO é lida como aprovação de plano
    (UC5, WSA-E5-T1). Ex.: 'Plano aprovado'."""
    return os.environ.get("JIRA_PLAN_APPROVED_STATUS", "Plan approved")


def get_plan_rejected_status() -> str:
    """Nome da coluna/status cuja transição é lida como REJEIÇÃO de plano
    (opcional). Ex.: 'Plano rejeitado'."""
    return os.environ.get("JIRA_PLAN_REJECTED_STATUS", "Plan rejected")


def get_poll_projects() -> list[str]:
    """Projetos Jira que o poller de fallback varre (CSV em
    `JIRA_POLL_PROJECTS`, ex.: 'DSE,OPS')."""
    raw = os.environ.get("JIRA_POLL_PROJECTS", "")
    return [p.strip() for p in raw.split(",") if p.strip()]
