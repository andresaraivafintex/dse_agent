"""WSA-E2-T1 — Verificação de assinatura (defesa #1 do pipeline de intake).

Nenhum evento downstream (ConversationEvent, admit_work_item, correlate) roda
sem `signature_verified=True`. Este módulo é puro (sem I/O) para ser
fácil de testar com um corpus de forgery — os adapters chamam estas funções
ANTES de qualquer outra coisa e retornam 401 + `dse_audit.emit(...)` se falhar.

Leitura de segredo: hoje via env var (`SLACK_SIGNING_SECRET`,
`GITHUB_WEBHOOK_SECRET`) — isto é temporário. Quando o backend de secrets do
WS-F (`services/platform/`, WSF-E2-T3a) existir, os adapters devem trocar
para lê-lo de lá (import opcional/defensivo, ver adapter_slack/config.py e
adapter_github/config.py).
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import NamedTuple

REPLAY_WINDOW_SECONDS = 5 * 60  # 5 minutos (WSA-E2-T1)


class SignatureCheck(NamedTuple):
    verified: bool
    reason: str  # "ok" | motivo de rejeição (para audit row)


def verify_slack_signature(
    *,
    signing_secret: str,
    timestamp_header: str | None,
    body: bytes,
    signature_header: str | None,
    now: float | None = None,
) -> SignatureCheck:
    """HMAC-SHA256 do signing secret sobre `v0:{timestamp}:{body}` (Slack
    Events API), com proteção de replay pela janela de timestamp.
    """
    if not signing_secret:
        return SignatureCheck(False, "missing_signing_secret")
    if not timestamp_header or not signature_header:
        return SignatureCheck(False, "missing_signature_headers")

    try:
        ts = float(timestamp_header)
    except ValueError:
        return SignatureCheck(False, "invalid_timestamp")

    now = time.time() if now is None else now
    if abs(now - ts) > REPLAY_WINDOW_SECONDS:
        return SignatureCheck(False, "timestamp_outside_replay_window")

    basestring = b"v0:" + timestamp_header.encode() + b":" + body
    digest = hmac.new(signing_secret.encode(), basestring, hashlib.sha256).hexdigest()
    computed = f"v0={digest}"

    if not hmac.compare_digest(computed, signature_header):
        return SignatureCheck(False, "signature_mismatch")

    return SignatureCheck(True, "ok")


def verify_github_signature(
    *,
    webhook_secret: str,
    body: bytes,
    signature_header: str | None,
) -> SignatureCheck:
    """HMAC-SHA256 do webhook secret sobre o corpo bruto (`X-Hub-Signature-256`)."""
    if not webhook_secret:
        return SignatureCheck(False, "missing_webhook_secret")
    if not signature_header:
        return SignatureCheck(False, "missing_signature_header")
    if not signature_header.startswith("sha256="):
        return SignatureCheck(False, "malformed_signature_header")

    digest = hmac.new(webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    computed = f"sha256={digest}"

    if not hmac.compare_digest(computed, signature_header):
        return SignatureCheck(False, "signature_mismatch")

    return SignatureCheck(True, "ok")


def verify_jira_signature(
    *,
    webhook_secret: str,
    body: bytes,
    signature_header: str | None,
) -> SignatureCheck:
    """WSA-E5-T1 — HMAC-SHA256 do webhook secret sobre o corpo bruto do
    webhook Jira Cloud (`X-Hub-Signature`, formato `sha256=<hex>`, o mesmo
    esquema do GitHub). Jira Cloud assina o payload com o segredo registrado
    ao criar o webhook via a REST API de webhooks dinâmicos.

    Sem um site Jira real registrado nesta sessão, o segredo é lido de
    `JIRA_WEBHOOK_SECRET` (env) como fallback do Vault (ver
    `adapter_jira.config`). A lógica é idêntica à de produção — só faltam as
    credenciais reais.
    """
    if not webhook_secret:
        return SignatureCheck(False, "missing_webhook_secret")
    if not signature_header:
        return SignatureCheck(False, "missing_signature_header")
    if not signature_header.startswith("sha256="):
        return SignatureCheck(False, "malformed_signature_header")

    digest = hmac.new(webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    computed = f"sha256={digest}"

    if not hmac.compare_digest(computed, signature_header):
        return SignatureCheck(False, "signature_mismatch")

    return SignatureCheck(True, "ok")
