"""Dev/mock OpenID Connect provider — FIXTURE CLARAMENTE MARCADO (não usar em
produção). Existe porque nenhum IdP real (Keycloak/Okta/Entra/Ping) está
provisionado nesta sessão de dev (ver README, seção "gaps"). Mina o mesmo tipo
de artefato que um IdP OIDC real emite — um `id_token` RS256 assinado + um
documento JWKS — para que `dse_platform.sso.OIDCVerifier` possa ser exercitado
contra o contrato OIDC REAL (assinatura RSA, claims `iss/aud/sub/exp`), não
contra um mock de si mesmo.

Em produção: apague este módulo do caminho e aponte `OIDCVerifier` para o
`.well-known/openid-configuration` / `jwks_uri` do IdP do cliente. A verificação
do lado do console (sso.py) não muda — só a origem das chaves.
"""
from __future__ import annotations

import time
import uuid

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


class DevIdP:
    """IdP OIDC de desenvolvimento. Gera um keypair RSA em memória e emite
    id_tokens assinados. `jwks()` devolve o material público no formato JWKS
    (o mesmo que um `jwks_uri` real serviria)."""

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
