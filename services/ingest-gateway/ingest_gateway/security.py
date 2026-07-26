"""WSA-E2-T1 — Signature verification (defense #1 of the intake pipeline).

No downstream event (ConversationEvent, admit_work_item, correlate) runs
without `signature_verified=True`. This module is pure (no I/O) so it is easy
to test against a forgery corpus — the adapters call these functions BEFORE
anything else and return 401 + `dse_audit.emit(...)` on failure.

Secret loading: today via env var (`SLACK_SIGNING_SECRET`,
`GITHUB_WEBHOOK_SECRET`) — this is temporary. Once the WS-F secrets backend
(`services/platform/`, WSF-E2-T3a) exists, the adapters must switch to reading
it from there (optional/defensive import, see adapter_slack/config.py and
adapter_github/config.py).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time
from typing import NamedTuple

REPLAY_WINDOW_SECONDS = 5 * 60  # 5 minutes (WSA-E2-T1)


class SignatureCheck(NamedTuple):
    verified: bool
    reason: str  # "ok" | rejection reason (for the audit row)


def verify_slack_signature(
    *,
    signing_secret: str,
    timestamp_header: str | None,
    body: bytes,
    signature_header: str | None,
    now: float | None = None,
) -> SignatureCheck:
    """HMAC-SHA256 of the signing secret over `v0:{timestamp}:{body}` (Slack
    Events API), with replay protection via the timestamp window.
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
    """HMAC-SHA256 of the webhook secret over the raw body (`X-Hub-Signature-256`)."""
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


def verify_teams_signature(
    *,
    shared_secret: str,
    body: bytes,
    authorization_header: str | None,
) -> SignatureCheck:
    """WSA (Phase 4, Teams adapter provision) — HMAC of the Microsoft Teams
    *outgoing webhook*.

    Teams signature scheme (documented by Microsoft): when an outgoing webhook
    is registered, Teams returns a Base64-encoded `securityToken`. On every
    POST, Teams sends the header
    `Authorization: HMAC <base64(HMAC_SHA256(decoded_secret, raw_body))>`.
    Verification: decode the secret from Base64 -> key bytes, compute the
    HMAC-SHA256 over the RAW body (not reparsed — TOCTOU defense), Base64-encode
    the digest and compare it in constant time with the header value.

    Unlike Slack/GitHub/Jira (hex in the header), Teams uses Base64 both for the
    secret and for the signature — hence a dedicated verifier. With no real
    Teams tenant in this session, the secret is read from env/Vault (see
    `adapter_teams.config`); the LOGIC is the production one.

    Note: the "full" Bot Framework channel (Azure Bot Service) authenticates via
    a JWT Bearer validated against Microsoft's OpenID metadata — heavier and
    network-dependent. For this provision, the outgoing webhook HMAC scheme is
    the direct analogue of Slack/Jira and is what this verifier covers; the JWT
    Bearer is documented as the activation step for the Bot Framework channel.
    """
    if not shared_secret:
        return SignatureCheck(False, "missing_shared_secret")
    if not authorization_header:
        return SignatureCheck(False, "missing_authorization_header")
    if not authorization_header.startswith("HMAC "):
        return SignatureCheck(False, "malformed_authorization_header")

    provided = authorization_header[len("HMAC "):].strip()

    try:
        key = base64.b64decode(shared_secret, validate=True)
    except (binascii.Error, ValueError):
        return SignatureCheck(False, "invalid_shared_secret_encoding")
    if not key:
        return SignatureCheck(False, "invalid_shared_secret_encoding")

    digest = hmac.new(key, body, hashlib.sha256).digest()
    computed = base64.b64encode(digest).decode()

    if not hmac.compare_digest(computed, provided):
        return SignatureCheck(False, "signature_mismatch")

    return SignatureCheck(True, "ok")


def verify_jira_signature(
    *,
    webhook_secret: str,
    body: bytes,
    signature_header: str | None,
) -> SignatureCheck:
    """WSA-E5-T1 — HMAC-SHA256 of the webhook secret over the raw body of the
    Jira Cloud webhook (`X-Hub-Signature`, format `sha256=<hex>`, the same
    scheme as GitHub). Jira Cloud signs the payload with the secret registered
    when the webhook was created via the dynamic webhooks REST API.

    With no real Jira site registered in this session, the secret is read from
    `JIRA_WEBHOOK_SECRET` (env) as a fallback for Vault (see
    `adapter_jira.config`). The logic is identical to production — only the
    real credentials are missing.
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
