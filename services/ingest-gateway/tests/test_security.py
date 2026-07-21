"""WSA-E2-T1 — corpus de forgery: sem assinatura, assinatura errada,
timestamp expirado, replay. 100% deve ser rejeitado."""
from __future__ import annotations

import base64
import hashlib
import hmac
import time

from ingest_gateway.security import (
    verify_github_signature,
    verify_slack_signature,
    verify_teams_signature,
)

SLACK_SECRET = "slack_signing_secret_test"
GITHUB_SECRET = "github_webhook_secret_test"
# Teams entrega o secret já em Base64 (é como o outgoing webhook o devolve).
TEAMS_SECRET_B64 = base64.b64encode(b"teams_outgoing_webhook_secret_test").decode()


def _slack_sig(secret: str, timestamp: str, body: bytes) -> str:
    basestring = b"v0:" + timestamp.encode() + b":" + body
    digest = hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    return f"v0={digest}"


def _github_sig(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

def test_slack_valid_signature_accepted():
    body = b'{"type":"event_callback"}'
    ts = str(int(time.time()))
    sig = _slack_sig(SLACK_SECRET, ts, body)
    result = verify_slack_signature(
        signing_secret=SLACK_SECRET, timestamp_header=ts, body=body, signature_header=sig
    )
    assert result.verified is True


def test_slack_missing_signature_rejected():
    body = b'{"type":"event_callback"}'
    ts = str(int(time.time()))
    result = verify_slack_signature(
        signing_secret=SLACK_SECRET, timestamp_header=ts, body=body, signature_header=None
    )
    assert result.verified is False


def test_slack_wrong_signature_rejected():
    body = b'{"type":"event_callback"}'
    ts = str(int(time.time()))
    result = verify_slack_signature(
        signing_secret=SLACK_SECRET, timestamp_header=ts, body=body, signature_header="v0=deadbeef"
    )
    assert result.verified is False
    assert result.reason == "signature_mismatch"


def test_slack_expired_timestamp_rejected():
    body = b'{"type":"event_callback"}'
    ts = str(int(time.time()) - 60 * 60)  # 1h atrás, fora da janela de 5min
    sig = _slack_sig(SLACK_SECRET, ts, body)
    result = verify_slack_signature(
        signing_secret=SLACK_SECRET, timestamp_header=ts, body=body, signature_header=sig
    )
    assert result.verified is False
    assert result.reason == "timestamp_outside_replay_window"


def test_slack_replay_of_valid_old_signature_rejected():
    """Um atacante capturou um par (timestamp, signature) válido no passado e
    tenta reenviar — mesmo com assinatura matematicamente correta para aquele
    timestamp, a janela de replay rejeita."""
    body = b'{"type":"event_callback"}'
    old_ts = str(int(time.time()) - 600)  # 10 minutos atrás
    sig = _slack_sig(SLACK_SECRET, old_ts, body)
    result = verify_slack_signature(
        signing_secret=SLACK_SECRET, timestamp_header=old_ts, body=body, signature_header=sig
    )
    assert result.verified is False


def test_slack_wrong_secret_rejected():
    body = b'{"type":"event_callback"}'
    ts = str(int(time.time()))
    sig = _slack_sig("wrong_secret", ts, body)
    result = verify_slack_signature(
        signing_secret=SLACK_SECRET, timestamp_header=ts, body=body, signature_header=sig
    )
    assert result.verified is False


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

def test_github_valid_signature_accepted():
    body = b'{"action":"opened"}'
    sig = _github_sig(GITHUB_SECRET, body)
    result = verify_github_signature(webhook_secret=GITHUB_SECRET, body=body, signature_header=sig)
    assert result.verified is True


def test_github_missing_signature_rejected():
    body = b'{"action":"opened"}'
    result = verify_github_signature(webhook_secret=GITHUB_SECRET, body=body, signature_header=None)
    assert result.verified is False


def test_github_wrong_signature_rejected():
    body = b'{"action":"opened"}'
    result = verify_github_signature(
        webhook_secret=GITHUB_SECRET, body=body, signature_header="sha256=deadbeef"
    )
    assert result.verified is False


def test_github_tampered_body_rejected():
    """Corpo alterado depois de assinado — assinatura não bate mais (garante
    que a verificação é sobre o corpo BRUTO, não uma versão reparseada)."""
    original_body = b'{"action":"opened"}'
    sig = _github_sig(GITHUB_SECRET, original_body)
    tampered_body = b'{"action":"closed"}'
    result = verify_github_signature(webhook_secret=GITHUB_SECRET, body=tampered_body, signature_header=sig)
    assert result.verified is False


def test_github_wrong_secret_rejected():
    body = b'{"action":"opened"}'
    sig = _github_sig("wrong_secret", body)
    result = verify_github_signature(webhook_secret=GITHUB_SECRET, body=body, signature_header=sig)
    assert result.verified is False


def test_github_malformed_header_rejected():
    body = b'{"action":"opened"}'
    result = verify_github_signature(webhook_secret=GITHUB_SECRET, body=body, signature_header="not-sha256-prefixed")
    assert result.verified is False


# ---------------------------------------------------------------------------
# Teams (provisão, Fase 4) — outgoing webhook HMAC (secret + assinatura Base64)
# ---------------------------------------------------------------------------
def _teams_auth(secret_b64: str, body: bytes) -> str:
    key = base64.b64decode(secret_b64)
    digest = hmac.new(key, body, hashlib.sha256).digest()
    return f"HMAC {base64.b64encode(digest).decode()}"


def test_teams_valid_signature_accepted():
    body = b'{"type":"message","text":"<at>DSE</at> ship it"}'
    auth = _teams_auth(TEAMS_SECRET_B64, body)
    result = verify_teams_signature(shared_secret=TEAMS_SECRET_B64, body=body, authorization_header=auth)
    assert result.verified is True


def test_teams_missing_header_rejected():
    body = b'{"type":"message"}'
    result = verify_teams_signature(shared_secret=TEAMS_SECRET_B64, body=body, authorization_header=None)
    assert result.verified is False
    assert result.reason == "missing_authorization_header"


def test_teams_malformed_header_rejected():
    body = b'{"type":"message"}'
    result = verify_teams_signature(
        shared_secret=TEAMS_SECRET_B64, body=body, authorization_header="Bearer abc"
    )
    assert result.verified is False
    assert result.reason == "malformed_authorization_header"


def test_teams_wrong_signature_rejected():
    body = b'{"type":"message"}'
    result = verify_teams_signature(
        shared_secret=TEAMS_SECRET_B64, body=body, authorization_header="HMAC deadbeef"
    )
    assert result.verified is False
    assert result.reason == "signature_mismatch"


def test_teams_tampered_body_rejected():
    original = b'{"type":"message","text":"approve low-risk change"}'
    auth = _teams_auth(TEAMS_SECRET_B64, original)
    tampered = b'{"type":"message","text":"approve high-risk change"}'
    result = verify_teams_signature(shared_secret=TEAMS_SECRET_B64, body=tampered, authorization_header=auth)
    assert result.verified is False


def test_teams_wrong_secret_rejected():
    body = b'{"type":"message"}'
    other = base64.b64encode(b"a_different_secret").decode()
    auth = _teams_auth(other, body)
    result = verify_teams_signature(shared_secret=TEAMS_SECRET_B64, body=body, authorization_header=auth)
    assert result.verified is False


def test_teams_non_base64_secret_rejected():
    body = b'{"type":"message"}'
    auth = "HMAC " + base64.b64encode(b"whatever").decode()
    result = verify_teams_signature(shared_secret="not*base64*secret", body=body, authorization_header=auth)
    assert result.verified is False
    assert result.reason == "invalid_shared_secret_encoding"
