from __future__ import annotations

import hashlib
import hmac

WEBHOOK_SECRET = "jira_webhook_secret_test"


def sign(body: bytes) -> str:
    """Jira Cloud X-Hub-Signature signature (HMAC-SHA256, `sha256=<hex>`
    format, the same scheme as GitHub's)."""
    digest = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"
