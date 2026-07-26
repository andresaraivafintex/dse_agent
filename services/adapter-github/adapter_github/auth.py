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

import jwt
import requests

GITHUB_API_BASE = "https://api.github.com"


def generate_app_jwt(app_id: str, private_key_pem: str, *, now: int | None = None) -> str:
    """App-level auth JWT (RS256), valid for <=10min as the GitHub API
    requires. `iat` is backdated 60s to tolerate clock drift."""
    ts = now if now is not None else int(time.time())
    payload = {"iat": ts - 60, "exp": ts + 9 * 60, "iss": app_id}
    return jwt.encode(payload, private_key_pem, algorithm="RS256")


def get_installation_access_token(
    *, app_id: str, private_key_pem: str, installation_id: str, session: requests.Session | None = None
) -> str:
    """Exchanges the App JWT for an installation access token (scoped to the
    installation, expires in ~1h) — this is the identity used to post comments
    (`adapter_github.backend.RealGithubClient`), never a personal PAT."""
    app_jwt = generate_app_jwt(app_id, private_key_pem)
    http = session or requests
    resp = http.post(
        f"{GITHUB_API_BASE}/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["token"]
