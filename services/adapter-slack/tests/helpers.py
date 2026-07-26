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


class Clock:
    """A `now`/`sleep` pair where sleeping ADVANCES time, plus a `monotonic` so it
    can stand in for the `time` module inside `adapter_slack.app`.

    Every budget assertion in these suites depends on this. The deadline check is
    `now() + delay > deadline`, so a `sleep` that records without advancing the
    clock leaves the budget permanently untouched and the assertion passes no
    matter what the code does — a test that cannot fail. It also keeps the
    endpoint's own `time.monotonic()` on the same clock as the client's, which a
    real `time.monotonic()` mixed with a fake `now` would not be.
    """

    def __init__(self, start: float = 1_000.0):
        self.t = start
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds
