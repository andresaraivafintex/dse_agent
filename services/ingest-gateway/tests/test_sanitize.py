"""WSA-E2-T3 — corpus with invisible characters and known credential patterns.
The original snapshot stays intact (auditing); only the sanitized version flows
down the pipeline."""
from __future__ import annotations

from ingest_gateway.sanitize import redact_secrets, sanitize_content, strip_invisible_unicode


def test_strips_zero_width_characters():
    text = "he​llo‌ wor‍ld﻿"
    assert strip_invisible_unicode(text) == "hello world"


def test_strips_bidi_override_characters():
    text = "normal‮text⁦more"
    cleaned = strip_invisible_unicode(text)
    assert "‮" not in cleaned
    assert "⁦" not in cleaned


def test_preserves_normal_whitespace_and_newlines():
    text = "line one\nline two\ttabbed"
    assert strip_invisible_unicode(text) == text


def test_redacts_github_token():
    text = "here is my token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 use it"
    redacted = redact_secrets(text)
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in redacted
    assert "[REDACTED:github_token]" in redacted


def test_redacts_slack_bot_token():
    text = "slack token xoxb-1234567890-abcdefghijklmnop end"
    redacted = redact_secrets(text)
    assert "xoxb-1234567890-abcdefghijklmnop" not in redacted
    assert "[REDACTED:slack_token]" in redacted


def test_redacts_aws_access_key_id():
    text = "AWS key: AKIAIOSFODNN7EXAMPLE thanks"
    redacted = redact_secrets(text)
    assert "AKIAIOSFODNN7EXAMPLE" not in redacted
    assert "[REDACTED:aws_access_key_id]" in redacted


def test_redacts_private_key_block():
    text = "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAK...\n-----END RSA PRIVATE KEY-----"
    redacted = redact_secrets(text)
    assert "MIIBOgIBAAJBAK" not in redacted
    assert "[REDACTED:private_key_block]" in redacted


def test_full_pipeline_combines_unicode_and_secret_redaction():
    text = "he​llo my token is ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 bye"
    sanitized = sanitize_content(text)
    assert "​" not in sanitized
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in sanitized
    assert "[REDACTED:github_token]" in sanitized


def test_benign_content_unaffected():
    text = "Please fix the bug in the login form, thanks!"
    assert sanitize_content(text) == text
