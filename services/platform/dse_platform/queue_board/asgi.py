"""Entrypoint ASGI do queue board para uvicorn (`uvicorn
dse_platform.queue_board.asgi:app --port 8890`).

Constrói o app com:
  - `TemporalSignalSender` real (alvo de `DSE_TEMPORAL_TARGET`, default
    `localhost:7233`) — exige `temporalio` instalado (extra `[temporal]`).
  - Um `OIDCVerifier` a partir de env, se configurado
    (`DSE_OIDC_ISSUER`, `DSE_OIDC_AUDIENCE`, `DSE_OIDC_JWKS` — JSON do JWKS, ou
    `DSE_OIDC_JWKS_FILE` apontando para um arquivo). Se não configurado, o
    login fica desabilitado (503) — apropriado para um deployment que ainda não
    plugou o IdP (ver README/gaps; em dev use o DevIdP nos testes).
"""
from __future__ import annotations

import json
import os

from ..sso import OIDCVerifier
from .app import create_app


def _build_verifier() -> OIDCVerifier | None:
    issuer = os.environ.get("DSE_OIDC_ISSUER")
    audience = os.environ.get("DSE_OIDC_AUDIENCE")
    jwks_raw = os.environ.get("DSE_OIDC_JWKS")
    jwks_file = os.environ.get("DSE_OIDC_JWKS_FILE")
    if not (issuer and audience and (jwks_raw or jwks_file)):
        return None
    if jwks_file:
        with open(jwks_file) as f:
            jwks = json.load(f)
    else:
        jwks = json.loads(jwks_raw)
    return OIDCVerifier(jwks=jwks, issuer=issuer, audience=audience)


app = create_app(verifier=_build_verifier())
