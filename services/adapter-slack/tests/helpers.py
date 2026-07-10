from __future__ import annotations

import hashlib
import hmac
import time

SIGNING_SECRET = "slack_signing_secret_test"


def sign(body: bytes, timestamp: str | None = None) -> tuple[str, str]:
    ts = timestamp or str(int(time.time()))
    basestring = b"v0:" + ts.encode() + b":" + body
    digest = hmac.new(SIGNING_SECRET.encode(), basestring, hashlib.sha256).hexdigest()
    return ts, f"v0={digest}"
