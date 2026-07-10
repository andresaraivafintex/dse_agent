"""Autenticação mínima de GitHub App: assina o JWT do app (RS256) e troca por
um installation access token — chamada REAL à API do GitHub (não um SDK de
terceiros pesado), usando `PyJWT` + `httpx`.

Se `services/adapter-github` (WS-A) já publicar um helper equivalente quando
este código for integrado, prefira reusá-lo (mesma lógica, evita duplicar a
gestão de token entre WS-A e WS-E) — ver README §Cross-workstream.

Credenciais: `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY` (conteúdo do PEM,
não um path — facilita injeção via secret manager/Vault), `GITHUB_APP_INSTALLATION_ID`.
Sem essas três env vars, `dse_validation.github.client.build_github_client()`
cai no `FakeGitHubClient` (modo local, ver client.py) em vez de tentar
autenticar — ver README para o que falta para produção.
"""
from __future__ import annotations

import time

import httpx
import jwt


def build_app_jwt(app_id: str, private_key_pem: str, ttl_seconds: int = 540) -> str:
    """JWT de app GitHub (válido no máx. 10min — usamos 9min de margem)."""
    now = int(time.time())
    payload = {
        "iat": now - 30,  # tolerância de clock skew recomendada pela doc do GitHub
        "exp": now + ttl_seconds,
        "iss": app_id,
    }
    return jwt.encode(payload, private_key_pem, algorithm="RS256")


def fetch_installation_token(
    app_id: str, private_key_pem: str, installation_id: str, api_base_url: str = "https://api.github.com"
) -> str:
    """Troca o JWT do app por um installation access token (expira em 1h)."""
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
