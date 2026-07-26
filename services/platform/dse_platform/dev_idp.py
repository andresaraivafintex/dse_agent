"""Dev/mock OpenID Connect provider — CLEARLY MARKED FIXTURE (do not use in
production). It exists because no real IdP (Keycloak/Okta/Entra/Ping) is
provisioned in this dev session (see README, "gaps" section). It mints the same
kind of artifact a real OIDC IdP emits — a signed RS256 `id_token` plus a JWKS
document — so that `dse_platform.sso.OIDCVerifier` can be exercised against the
REAL OIDC contract (RSA signature, `iss/aud/sub/exp` claims) rather than against
a mock of itself.

In production: drop this module from the path and point `OIDCVerifier` at the
customer IdP's `.well-known/openid-configuration` / `jwks_uri`. Verification on
the console side (sso.py) does not change — only where the keys come from.
"""
from __future__ import annotations

import time
import uuid

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


class DevIdP:
    """Development OIDC IdP. Generates an in-memory RSA keypair and mints signed
    id_tokens. `jwks()` returns the public material in JWKS format (the same a
    real `jwks_uri` would serve)."""

    def __init__(self, issuer: str = "https://dev-idp.local", kid: str = "dev-key-1"):
        self.issuer = issuer
        self.kid = kid
        self._private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._private_pem = self._private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

    def jwks(self) -> dict:
        from jwt.algorithms import RSAAlgorithm

        pub = self._private_key.public_key()
        jwk = RSAAlgorithm.to_jwk(pub, as_dict=True)
        jwk["kid"] = self.kid
        jwk["use"] = "sig"
        jwk["alg"] = "RS256"
        return {"keys": [jwk]}

    def mint_id_token(
        self,
        *,
        subject: str,
        audience: str,
        email: str | None = None,
        name: str | None = None,
        ttl_seconds: int = 3600,
        extra_claims: dict | None = None,
    ) -> str:
        now = int(time.time())
        claims = {
            "iss": self.issuer,
            "sub": subject,
            "aud": audience,
            "iat": now,
            "exp": now + ttl_seconds,
            "jti": uuid.uuid4().hex,
        }
        if email is not None:
            claims["email"] = email
        if name is not None:
            claims["name"] = name
        if extra_claims:
            claims.update(extra_claims)
        return jwt.encode(claims, self._private_pem, algorithm="RS256", headers={"kid": self.kid})
