from __future__ import annotations

import hashlib
import hmac

WEBHOOK_SECRET = "jira_webhook_secret_test"


def sign(body: bytes) -> str:
    """Assinatura X-Hub-Signature do Jira Cloud (HMAC-SHA256, formato
    `sha256=<hex>`, o mesmo esquema do GitHub)."""
    digest = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"
