"""Rate-limit backoff for the outbound GitHub calls (`backend.RealGithubClient`,
`auth.get_installation_access_token`).

GitHub throttles in two different shapes and neither of them is a plain 429:

  * PRIMARY limit — `403` (sometimes `429`) with `x-ratelimit-remaining: 0` and
    `x-ratelimit-reset` carrying the epoch second at which the quota refills.
    There is no `Retry-After` here, so the reset header IS the retry hint.
  * SECONDARY / abuse limit — `403` or `429` with `Retry-After` in seconds.

A bare `403` with NEITHER marker is a permission problem (App not installed on
the repo, comment locked). Retrying that burns the sweep budget and never
succeeds, so the classifier requires one of the two markers before anything is
retried — every other status goes straight back to the caller, which keeps
`raise_for_status()` as the single place non-throttle errors are handled.

THE BUDGET BELONGS TO THE CALLER, NOT TO THE CALL
-------------------------------------------------
`request_with_backoff` takes an absolute `deadline` and has NO default, because
a per-call budget bounds nothing that anybody waits on. `/internal/reconcile`
walks every pending thread inside ONE HTTP request and `list_issue_comments`
pages up to five times per thread, so a per-call budget entitles a single sweep
to dozens of independent budgets — far past the 120s the CronJob waits. Whoever
calls GitHub knows when its own caller gives up and passes that instant in; one
deadline shared by the token exchange and every request of one sweep is the only
scope in which "we will not overrun our caller" is a true statement.

A HINT WE CANNOT AFFORD IS NOT SHORTENED
----------------------------------------
`x-ratelimit-reset` 40 minutes out means the quota refills in 40 minutes.
Sleeping 30s and re-requesting is not a partial retry, it is an early poke into a
quota that provably has not refilled — and on the SECONDARY/abuse limit GitHub
documents that requesting before `Retry-After` elapses can EXTEND the block. So a
hint that does not fit in the remaining budget makes the call give up on the
spot, leaving the retry to whoever calls us next.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable

logger = logging.getLogger("adapter_github.ratelimit")

MAX_ATTEMPTS = 4
BASE_BACKOFF_S = 1.0


class GithubRateLimited(RuntimeError):
    """The retry budget ran out while GitHub was still throttling.

    A dedicated type (not the `requests.HTTPError` a `raise_for_status()` would
    give) so the callers that already swallow per-item failures — the reconciler
    sweep — can tell "GitHub told us to slow down" apart from "this request was
    malformed" in the logs without parsing a status code out of a message.
    """

    def __init__(self, message: str, *, response: Any = None):
        super().__init__(message)
        self.response = response


def _header(response: Any, name: str) -> str | None:
    """Case-insensitive header read that also works on fake transports.

    `requests` hands back a `CaseInsensitiveDict`, but the fake responses in the
    tests (and any hand-built stub) carry a plain dict, where
    `headers["Retry-After"]` and `headers["retry-after"]` are different keys.
    """
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    value = headers.get(name)
    if value is None:
        lowered = {str(k).lower(): v for k, v in dict(headers).items()}
        value = lowered.get(name.lower())
    return None if value is None else str(value)


def _as_number(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw.strip())
    except (ValueError, AttributeError):
        return None  # GitHub sends integers; anything else is treated as "no hint"


def is_rate_limited(response: Any) -> bool:
    """True only for the two throttle shapes documented at the top of this
    module. Everything else (404, 422, 5xx, a permission 403) is NOT retried."""
    status = getattr(response, "status_code", None)
    if status not in (403, 429):
        return False
    if _header(response, "x-ratelimit-remaining") == "0":
        return True
    return _header(response, "retry-after") is not None


def retry_delay_s(response: Any, *, attempt: int, now: float) -> float:
    """How long to wait before repeating the call, in seconds.

    Server hint first (`Retry-After`, then `x-ratelimit-reset`), honoured
    VERBATIM — never trimmed to something we would rather wait (see the module
    docstring). Only when GitHub gave no usable number do we fall back to
    exponential backoff with jitter — the jitter matters because every replica of
    this adapter shares one installation quota and would otherwise wake up in
    lockstep.
    """
    hinted = _as_number(_header(response, "retry-after"))
    if hinted is None and _header(response, "x-ratelimit-remaining") == "0":
        reset_at = _as_number(_header(response, "x-ratelimit-reset"))
        if reset_at is not None:
            # +1s: the quota refills AT `reset`, and our clock can sit slightly
            # ahead of GitHub's — waking up a hair early costs another 403.
            hinted = max(reset_at - now, 0.0) + 1.0
    if hinted is not None:
        return max(hinted, 0.0)
    return BASE_BACKOFF_S * (2 ** attempt) * random.uniform(0.5, 1.0)


def request_with_backoff(
    send: Callable[[], Any],
    *,
    what: str,
    deadline: float,
    max_attempts: int = MAX_ATTEMPTS,
) -> Any:
    """Calls `send()` and repeats it while GitHub is throttling, for as long as
    `deadline` allows.

    `deadline` is an absolute `time.time()` reading, required and meant to be
    SHARED: pass the same value to every request of one sweep, or the budget
    quietly becomes per-request again and bounds nothing. The clock is wall clock
    rather than monotonic because `x-ratelimit-reset` is an epoch second and the
    delay math already has to live in that clock — one clock for both keeps the
    two comparisons commensurable.

    The clock and the sleep are read off the module-level `time` here rather than
    injected. Injectable defaults are bound once at def time, so a test that
    patched `adapter_github.ratelimit.time.sleep` and forgot to also pass
    `sleep=...` patched nothing and slept for real — which is the same reason a
    budget assertion could pass against code that ignored the budget. Tests swap
    the whole module with `monkeypatch.setattr(ratelimit, "time", clock)`.

    `send` is repeated VERBATIM, which is only sound because the retry path is
    entered exclusively on a throttle response: a request GitHub refused with
    403/429 never reached the resource, so it has no side effect to duplicate.
    Timeouts, connection resets and 5xx — the cases where a `POST .../comments`
    may well have been applied before the answer got lost — propagate untouched
    rather than posting the same status comment twice.
    """
    attempts = 0
    response: Any = None
    for attempt in range(max_attempts):
        attempts += 1
        response = send()
        if not is_rate_limited(response):
            return response
        if attempt == max_attempts - 1:
            break
        delay = retry_delay_s(response, attempt=attempt, now=time.time())
        if time.time() + delay > deadline:
            # Out of budget. Waiting less than GitHub asked for and requesting
            # again is what extends an abuse block; give up instead.
            break
        logger.warning(
            "github rate limited on %s (status=%s), sleeping %.1fs (attempt %d/%d)",
            what, getattr(response, "status_code", None), delay, attempt + 1, max_attempts,
        )
        time.sleep(delay)
    raise GithubRateLimited(
        f"github still rate limiting {what} after {attempts} attempt(s), "
        f"{max(deadline - time.time(), 0.0):.1f}s of budget left",
        response=response,
    )
