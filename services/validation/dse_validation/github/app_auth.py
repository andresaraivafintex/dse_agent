"""Minimal GitHub App authentication: signs the app JWT (RS256) and exchanges it
for an installation access token — a REAL call to the GitHub API (not a heavy
third-party SDK), using `PyJWT` + `httpx`.

If `services/adapter-github` (WS-A) has already published an equivalent helper by
the time this code is integrated, prefer reusing it (same logic, avoids
duplicating token management between WS-A and WS-E) — see README §Cross-workstream.

Credentials: `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY` (the PEM contents, not a
path — makes injection via secret manager/Vault easier), `GITHUB_APP_INSTALLATION_ID`.
Without those three env vars, `dse_validation.github.client.build_github_client()`
falls back to `FakeGitHubClient` (local mode, see client.py) instead of trying to
authenticate — see README for what is still missing for production.
"""
from __future__ import annotations

import time

import httpx
import jwt


def build_app_jwt(app_id: str, private_key_pem: str, ttl_seconds: int = 540) -> str:
    """GitHub app JWT (valid for at most 10min — we use 9min for margin)."""
    now = int(time.time())
    payload = {
        "iat": now - 30,  # clock skew tolerance recommended by the GitHub docs
        "exp": now + ttl_seconds,
        "iss": app_id,
    }
    return jwt.encode(payload, private_key_pem, algorithm="RS256")


def fetch_installation_token(
    app_id: str, private_key_pem: str, installation_id: str, api_base_url: str = "https://api.github.com"
) -> str:
    """Exchanges the app JWT for an installation access token (expires in 1h)."""
    app_jwt = build_app_jwt(app_id, private_key_pem)
    resp = httpx.post(
        f"{api_base_url}/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=15.0,
    )
    resp.raise_for_status()
    return resp.json()["token"]
