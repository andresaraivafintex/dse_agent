from __future__ import annotations

import hashlib
import hmac

WEBHOOK_SECRET = "github_webhook_secret_test"


def sign(body: bytes) -> str:
    digest = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class Clock:
    """A `now`/`sleep` pair where sleeping ADVANCES time, plus a `time` alias so
    it can stand in for the `time` module inside `adapter_github.app`.

    Every budget assertion in these suites depends on this. The deadline check is
    `now() + delay > deadline`, so a `sleep` that records without advancing the
    clock leaves the budget permanently untouched and the assertion passes no
    matter what the code does — a test that cannot fail. It also keeps the
    endpoint's own `time.time()` on the same clock as the client's, which a real
    `time.time()` mixed with a fake `now` would not be.

    `start` is a plausible epoch second because `retry_delay_s` compares it
    against `x-ratelimit-reset`.
    """

    def __init__(self, start: float = 1_800_000_000.0):
        self.t = start
        self.slept: list[float] = []

    def time(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds
