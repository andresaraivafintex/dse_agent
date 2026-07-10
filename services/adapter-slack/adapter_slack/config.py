"""Configuração do adapter Slack — leitura de credenciais.

Ordem de resolução (WSA-E2-T1): backend de secrets do WS-F
(`dse_secrets`, WSF-E2-T3a) PRIMEIRO se disponível e responder; senão cai
para env var local (`SLACK_BOT_TOKEN`/`SLACK_SIGNING_SECRET`). O import de
`dse_secrets` é opcional/defensivo — funciona mesmo se `services/platform`
não estiver instalado neste ambiente (ex.: CI rodando só o WS-A isolado).

Nesta sessão de desenvolvimento `services/platform/dse_secrets` (WSF-E2-T3a)
já existe e é usado de verdade — mas nenhum Slack App real foi registrado,
então o path `dse/slack/webhook` no Vault dev não tem uma versão gravada; a
leitura cai (de propósito, via `VaultUnavailableError`) para as env vars
abaixo, que devem ser preenchidas com um valor de teste local para rodar os
testes/fixtures deste serviço.
"""
from __future__ import annotations

import os

def get_tenant_id() -> str:
    """Fase 1: single-tenant de desenvolvimento (lido a cada chamada, não
    fixado no import — permite overrides em teste via env var mesmo depois
    do módulo já ter sido importado). `ConversationEvent` não carrega
    `tenant_id` (é um conceito de plataforma/workspace->tenant mapping) —
    mapear múltiplos workspaces Slack para tenants distintos é escopo de
    WS-F/Fase 2 (identity map completo). Documentado como limitação
    conhecida no README."""
    return os.environ.get("DSE_TENANT_ID", "tenant_dev")


def get_slack_bot_token() -> str:
    try:
        from dse_secrets import VaultUnavailableError, get_secret

        try:
            return get_secret("dse/slack/webhook")["bot_token"]
        except VaultUnavailableError:
            pass
    except ImportError:
        pass
    return os.environ.get("SLACK_BOT_TOKEN", "")


def get_slack_signing_secret() -> str:
    try:
        from dse_secrets import VaultUnavailableError, get_secret

        try:
            return get_secret("dse/slack/webhook")["signing_secret"]
        except VaultUnavailableError:
            pass
    except ImportError:
        pass
    return os.environ.get("SLACK_SIGNING_SECRET", "")
