"""Queue board ASGI entrypoint for uvicorn (`uvicorn
dse_platform.queue_board.asgi:app --port 8890`).

Builds the app with:
  - A real `TemporalSignalSender` (target from `DSE_TEMPORAL_TARGET`, default
    `localhost:7233`) — requires `temporalio` installed (extra `[temporal]`).
  - An `OIDCVerifier` built from env, if configured
    (`DSE_OIDC_ISSUER`, `DSE_OIDC_AUDIENCE`, `DSE_OIDC_JWKS` — the JWKS JSON, or
    `DSE_OIDC_JWKS_FILE` pointing at a file). If not configured, login is
    disabled (503) — appropriate for a deployment that has not plugged in the
    IdP yet (see README/gaps; in dev, use the DevIdP in the tests).
"""
from __future__ import annotations

import json
import os

from ..sso import OIDCVerifier
from .app import create_app


def _discover_jwks_uri(issuer: str) -> str | None:
    """Read `jwks_uri` from the issuer's OIDC discovery document.

    Preferred over pasting a JWKS into configuration: the key set is the one
    thing in an OIDC setup that is *expected* to change without notice, so
    copying it into an env var makes a routine provider rotation look like a
    total authentication outage.
    """
    import urllib.request

    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.load(resp).get("jwks_uri")
    except Exception:  # noqa: BLE001 — fall back to an explicit JWKS below
        return None


def _build_verifier() -> OIDCVerifier | None:
    issuer = os.environ.get("DSE_OIDC_ISSUER")
    audience = os.environ.get("DSE_OIDC_AUDIENCE")
    jwks_raw = os.environ.get("DSE_OIDC_JWKS")
    jwks_file = os.environ.get("DSE_OIDC_JWKS_FILE")
    jwks_uri = os.environ.get("DSE_OIDC_JWKS_URI")

    if not (issuer and audience):
        return None

    # Discovery first: with only issuer + audience configured, the key set is
    # fetched from the provider and refreshed automatically when it rotates.
    # An explicitly supplied JWKS still wins, which is what the tests and the
    # DevIdP rely on — they have no discovery endpoint to read.
    if not (jwks_raw or jwks_file):
        jwks_uri = jwks_uri or _discover_jwks_uri(issuer)
        if not jwks_uri:
            return None
        try:
            import urllib.request

            with urllib.request.urlopen(jwks_uri, timeout=10) as resp:
                jwks = json.load(resp)
        except Exception:  # noqa: BLE001
            # Starting with login disabled (503) is better than starting with a
            # verifier that trusts nothing: the failure names itself instead of
            # looking like everyone's credentials went bad at once.
            return None
        return OIDCVerifier(jwks=jwks, issuer=issuer, audience=audience, jwks_uri=jwks_uri)

    if jwks_file:
        with open(jwks_file) as f:
            jwks = json.load(f)
    else:
        jwks = json.loads(jwks_raw)
    return OIDCVerifier(jwks=jwks, issuer=issuer, audience=audience, jwks_uri=jwks_uri)


app = create_app(verifier=_build_verifier())
