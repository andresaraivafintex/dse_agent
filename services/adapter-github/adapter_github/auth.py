"""Autenticação como GitHub App (WSA-E4-T2) — NUNCA token pessoal. Fluxo
real (RFC 7519 JWT assinado RS256 com a chave privada da App, trocado por
um installation access token de curta duração via a API real do GitHub).

Sem GitHub App real registrada nesta sessão: a lógica abaixo é exatamente o
fluxo que rodaria em produção (`PyJWT` + `requests` contra
`api.github.com`); só faltam os valores reais de `GITHUB_APP_ID`/
`GITHUB_APP_PRIVATE_KEY`/`GITHUB_APP_INSTALLATION_ID` (ver
`adapter_github.config` e README — o que falta para produção).
"""
from __future__ import annotations

import time

import jwt
import requests

GITHUB_API_BASE = "https://api.github.com"


def generate_app_jwt(app_id: str, private_key_pem: str, *, now: int | None = None) -> str:
    """JWT de App-level auth (RS256), válido por <=10min conforme a API do
    GitHub exige. `iat` com 60s de folga para tolerar clock drift."""
    ts = now if now is not None else int(time.time())
    payload = {"iat": ts - 60, "exp": ts + 9 * 60, "iss": app_id}
    return jwt.encode(payload, private_key_pem, algorithm="RS256")


def get_installation_access_token(
    *, app_id: str, private_key_pem: str, installation_id: str, session: requests.Session | None = None
) -> str:
    """Troca o JWT de App por um installation access token (escopo limitado
    à instalação, expira em ~1h) — esta é a identidade usada para postar
    comentários (`adapter_github.backend.RealGithubClient`), nunca um PAT
    pessoal."""
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
