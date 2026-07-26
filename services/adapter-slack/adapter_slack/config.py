"""Slack adapter configuration — credential lookup.

Resolution order (WSA-E2-T1): the WS-F secrets backend (`dse_secrets`,
WSF-E2-T3a) FIRST if it is available and responds; otherwise it falls back
to a local env var (`SLACK_BOT_TOKEN`/`SLACK_SIGNING_SECRET`). The
`dse_secrets` import is optional/defensive — this works even if
`services/platform` is not installed in this environment (e.g. CI running
only WS-A in isolation).

In this development session `services/platform/dse_secrets` (WSF-E2-T3a)
already exists and is genuinely used — but no real Slack App was registered,
so the `dse/slack/webhook` path in the dev Vault has no version written; the
read falls through (on purpose, via `VaultUnavailableError`) to the env vars
below, which must be filled with a local test value to run this service's
tests/fixtures.
"""
from __future__ import annotations

import os

def get_tenant_id() -> str:
    """Phase 1: development single-tenant (read on every call, not frozen at
    import time — this allows test overrides via env var even after the
    module has already been imported). `ConversationEvent` does not carry
    `tenant_id` (that is a platform/workspace->tenant mapping concept) —
    mapping multiple Slack workspaces to distinct tenants is WS-F/Phase 2
    scope (the full identity map). Documented as a known limitation in the
    README."""
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
