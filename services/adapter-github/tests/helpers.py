from __future__ import annotations

import hashlib
import hmac

WEBHOOK_SECRET = "github_webhook_secret_test"


def sign(body: bytes) -> str:
    digest = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"
