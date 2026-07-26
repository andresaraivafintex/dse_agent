"""Authentication as a GitHub App (WSA-E4-T2) — NEVER a personal token. Real
flow (RFC 7519 JWT signed RS256 with the App's private key, exchanged for a
short-lived installation access token via the real GitHub API).

With no real GitHub App registered in this session: the logic below is exactly
the flow that would run in production (`PyJWT` + `requests` against
`api.github.com`); only the real values of `GITHUB_APP_ID`/
`GITHUB_APP_PRIVATE_KEY`/`GITHUB_APP_INSTALLATION_ID` are missing (see
`adapter_github.config` and README — what is still missing for production).
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import jwt
import requests

from .ratelimit import request_with_backoff

GITHUB_API_BASE = "https://api.github.com"

#: Minted installation tokens, keyed by (app_id, installation_id) -> (token,
#: expiry as epoch seconds).
#:
#: Without a cache, EVERY caller of `build_real_github_client` pays a full token
#: exchange first: one per `/internal/status-comment` request (the orchestrator
#: posts one on every state transition) and one per reconciler sweep. That
#: round-trip can itself be throttled, so the request could spend its budget
#: before even reaching the call it was made for. The key is a pair because one
#: process may legitimately serve more than one installation.
_TOKEN_CACHE: dict[tuple[str, str], tuple[str, float]] = {}

#: Mint a new token this long before the current one expires. A token that is
#: still valid when handed out but expires mid-sweep would fail every remaining
#: request with a 401 that no retry can fix, and installation tokens live about
#: an hour, so giving up the last five minutes of one costs nothing.
_TOKEN_REFRESH_MARGIN_S = 300.0


def generate_app_jwt(app_id: str, private_key_pem: str, *, now: int | None = None) -> str:
    """App-level auth JWT (RS256), valid for <=10min as the GitHub API
    requires. `iat` is backdated 60s to tolerate clock drift."""
    ts = now if now is not None else int(time.time())
    payload = {"iat": ts - 60, "exp": ts + 9 * 60, "iss": app_id}
    return jwt.encode(payload, private_key_pem, algorithm="RS256")


def _expires_at_epoch(raw: Any) -> float | None:
    """GitHub's `expires_at` (`"2026-07-26T15:04:05Z"`) as epoch seconds.

    None when the field is absent, not a string, unparseable, or carries no
    timezone — a naive timestamp would be read as LOCAL time, which on a UTC-
    offset host means caching a token as valid hours after it died. The caller
    treats None as "use it once, do not cache": a cached token with a guessed
    lifetime is a 401 nobody can retry their way out of.
    """
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def get_installation_access_token(
    *, app_id: str, private_key_pem: str, installation_id: str, deadline: float,
    session: requests.Session | None = None
) -> str:
    """Exchanges the App JWT for an installation access token (scoped to the
    installation, expires in ~1h) — this is the identity used to post comments
    (`adapter_github.backend.RealGithubClient`), never a personal PAT.

    Cached in `_TOKEN_CACHE` until it is within `_TOKEN_REFRESH_MARGIN_S` of
    GitHub's own `expires_at`, so a burst of status-comment requests pays for one
    exchange instead of one each. `deadline` is the caller's budget for the whole
    operation and is shared with the client this token authenticates."""
    cache_key = (app_id, installation_id)
    cached = _TOKEN_CACHE.get(cache_key)
    if cached is not None and time.time() + _TOKEN_REFRESH_MARGIN_S < cached[1]:
        return cached[0]

    app_jwt = generate_app_jwt(app_id, private_key_pem)
    http = session or requests
    # Retried on throttle only, and safe to repeat: a refused exchange minted no
    # token, and even a duplicate would just be another short-lived credential —
    # GitHub does not invalidate the previous one. Worth retrying at all because
    # this call gates EVERY outbound comment: one 429 here and the whole
    # reconciler sweep posts nothing.
    resp = request_with_backoff(
        lambda: http.post(
            f"{GITHUB_API_BASE}/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=10,
        ),
        what="installation_access_token",
        deadline=deadline,
    )
    resp.raise_for_status()
    payload = resp.json()
    token = payload["token"]
    expires_at = _expires_at_epoch(payload.get("expires_at"))
    # No lock: FastAPI runs the sync endpoints in a threadpool, and the worst a
    # race here does is mint one extra token — GitHub keeps the previous one
    # valid. A lock would serialize every outbound comment for that.
    if expires_at is None:
        _TOKEN_CACHE.pop(cache_key, None)
    else:
        _TOKEN_CACHE[cache_key] = (token, expires_at)
    return token
