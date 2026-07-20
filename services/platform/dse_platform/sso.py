"""WSF-E3-T3 — SSO/OIDC do console admin + identidade de console (ADR-22).

Duas responsabilidades:
1. `OIDCVerifier` — valida um `id_token` (RS256) contra um JWKS (do IdP real em
   produção; do `dev_idp.DevIdP` em dev/teste — ver README). Verifica assinatura,
   `iss`, `aud`, `exp`. Nunca confia num token não verificado (P8).
2. Ligação com o identity map da fundação + offboarding: `login` resolve o
   `sub` do IdP para um `principal_id` (via `dse_identity.resolve_principal`
   com plataforma "sso"), garante uma linha em `dse_console_identity`, e RECUSA
   o login se a conta estiver offboardada (`active = false`) ou expirada
   (contractor além de `expires_at`). `offboard` desativa a conta — o que
   também a remove da resolução de approver/steering (ver access_bundles e
   steering_resolution).

Account matching (ADR-22): por `sub` (subject) estável do IdP, NÃO por email
(email pode ser reatribuído). O email é guardado só para exibição/contato.
"""
from __future__ import annotations

import dataclasses
import os
import uuid
from typing import Any

import jwt
import psycopg2
import psycopg2.extras
from dse_audit import emit

_DSN = os.environ.get(
    "DSE_PLATFORM_DATABASE_URL",
    os.environ.get("DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"),
)


def _get_connection():
    return psycopg2.connect(_DSN)


def ensure_sso_principal(sso_subject: str, *, display_name: str | None = None, conn=None) -> str:
    """Account matching do ADR-22 para usuários de SSO: resolve (ou mina) o
    `principal_id` de um `sso_subject`.

    NOTA DE FUNDAÇÃO (documentada no README + ADR-22): o `identity_links` da
    fundação tem um CHECK que só admite `platform IN ('slack','github','jira')`
    — não podemos gravar `platform = 'sso'` lá (e não editamos a migração da
    fundação). Portanto o principal de um usuário de SSO é criado DIRETO em
    `principals`, e a chave de account-matching (`sso_subject`) vive em
    `dse_console_identity`. Consumidores continuam vendo um `usr_<uuid>` normal
    em `principals` — a assinatura pública (`principal_id`) não muda.

    Idempotente por `sso_subject` (a linha de console_identity é a fonte da
    verdade do matching)."""
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT principal_id FROM dse_console_identity WHERE sso_subject = %s",
                (sso_subject,),
            )
            row = cur.fetchone()
            if row is not None:
                return row[0]
            principal_id = f"usr_{uuid.uuid4().hex[:16]}"
            cur.execute(
                "INSERT INTO principals (id, display_name) VALUES (%s, %s)",
                (principal_id, display_name),
            )
        if owns:
            conn.commit()
        return principal_id
    except Exception:
        if owns:
            conn.rollback()
        raise
    finally:
        if owns:
            conn.close()


class InvalidToken(Exception):
    """id_token falhou a verificação (assinatura/iss/aud/exp)."""


class LoginDenied(Exception):
    """Token válido mas a conta de console está offboardada/expirada."""


@dataclasses.dataclass(frozen=True)
class OIDCClaims:
    subject: str
    audience: str
    issuer: str
    email: str | None
    name: str | None
    raw: dict[str, Any]


class OIDCVerifier:
    """Verificador de id_token OIDC. Construa com o JWKS do IdP (dict no formato
    `{"keys": [...]}`) e o `issuer`/`audience` (client_id) esperados. Em
    produção, `jwks` vem do `jwks_uri` do IdP (buscado e cacheado pelo caller);
    o contrato de verificação é idêntico ao do dev IdP."""

    def __init__(self, *, jwks: dict, issuer: str, audience: str):
        self._issuer = issuer
        self._audience = audience
        self._keys = {}
        for jwk in jwks.get("keys", []):
            kid = jwk.get("kid")
            self._keys[kid] = jwt.PyJWK.from_dict(jwk)

    def verify(self, id_token: str) -> OIDCClaims:
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.InvalidTokenError as e:
            raise InvalidToken(f"header inválido: {e}") from e
        kid = header.get("kid")
        signing = self._keys.get(kid)
        if signing is None:
            raise InvalidToken(f"kid {kid!r} desconhecido no JWKS")
        try:
            claims = jwt.decode(
                id_token,
                signing.key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.ExpiredSignatureError as e:
            raise InvalidToken(f"token expirado: {e}") from e
        except jwt.InvalidTokenError as e:
            raise InvalidToken(f"token inválido: {e}") from e
        return OIDCClaims(
            subject=claims["sub"],
            audience=self._audience,
            issuer=self._issuer,
            email=claims.get("email"),
            name=claims.get("name"),
            raw=claims,
        )


@dataclasses.dataclass(frozen=True)
class ConsoleSession:
    principal_id: str
    sso_subject: str
    email: str | None
    display_name: str | None
    tenant_id: str | None
    roles: list[str]


def login(verifier: OIDCVerifier, id_token: str, *, conn=None) -> ConsoleSession:
    """Verifica o token, resolve/garante a identidade de console e devolve a
    sessão. Levanta `LoginDenied` se offboardado/expirado (P6: falha limpa).
    Grava audit `console_login` (ou `console_login_denied`)."""
    claims = verifier.verify(id_token)

    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT * FROM dse_console_identity WHERE sso_subject = %s",
                (claims.subject,),
            )
            row = cur.fetchone()

        if row is not None and (
            not row["active"]
            or (row["expires_at"] is not None and _expired(row["expires_at"]))
        ):
            emit(
                actor=row["principal_id"], action="console_login_denied",
                tenant_id=row["tenant_id"] or "platform",
                details={"reason": "offboarded_or_expired", "sso_subject": claims.subject},
                conn=conn,
            )
            if owns:
                conn.commit()
            raise LoginDenied(f"conta de console offboardada/expirada (sub={claims.subject})")

        if row is None:
            # primeira aparição: mina o principal (account matching por subject)
            principal_id = ensure_sso_principal(claims.subject, display_name=claims.name, conn=conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO dse_console_identity (principal_id, sso_subject, email, display_name)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (sso_subject) DO NOTHING
                    """,
                    (principal_id, claims.subject, claims.email, claims.name),
                )
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("SELECT * FROM dse_console_identity WHERE sso_subject = %s", (claims.subject,))
                row = cur.fetchone()

        session = ConsoleSession(
            principal_id=row["principal_id"],
            sso_subject=row["sso_subject"],
            email=row["email"],
            display_name=row["display_name"],
            tenant_id=row["tenant_id"],
            roles=list(row["roles"] or []),
        )
        emit(
            actor=session.principal_id, action="console_login",
            tenant_id=session.tenant_id or "platform",
            details={"sso_subject": claims.subject, "roles": session.roles},
            conn=conn,
        )
        if owns:
            conn.commit()
        return session
    except Exception:
        if owns:
            conn.rollback()
        raise
    finally:
        if owns:
            conn.close()


def _expired(ts) -> bool:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts < now


def provision_console_user(
    principal_id: str,
    sso_subject: str,
    *,
    email: str | None = None,
    display_name: str | None = None,
    tenant_id: str | None = None,
    roles: list[str] | None = None,
    is_contractor: bool = False,
    expires_at=None,
    actor: str = "system:platform-admin",
    conn=None,
) -> None:
    """Provisiona/atualiza uma identidade de console explicitamente (ex.: admin
    concede papel `approver`/`operator` antes do primeiro login). `principal_id`
    deve existir em `principals` (resolvido via dse_identity)."""
    if roles is not None:
        for r in roles:
            if r not in {"operator", "approver", "viewer", "admin"}:
                raise ValueError(f"role desconhecido: {r!r}")
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO dse_console_identity
                    (principal_id, sso_subject, email, display_name, tenant_id, roles, is_contractor, expires_at)
                VALUES (%s, %s, %s, %s, %s, COALESCE(%s::jsonb, '[]'::jsonb), %s, %s)
                ON CONFLICT (sso_subject) DO UPDATE SET
                    email = COALESCE(EXCLUDED.email, dse_console_identity.email),
                    display_name = COALESCE(EXCLUDED.display_name, dse_console_identity.display_name),
                    tenant_id = COALESCE(EXCLUDED.tenant_id, dse_console_identity.tenant_id),
                    roles = EXCLUDED.roles,
                    is_contractor = EXCLUDED.is_contractor,
                    expires_at = EXCLUDED.expires_at
                """,
                (
                    principal_id, sso_subject, email, display_name, tenant_id,
                    None if roles is None else psycopg2.extras.Json(roles),
                    is_contractor, expires_at,
                ),
            )
        emit(
            actor=actor, action="console_user_provisioned",
            tenant_id=tenant_id or "platform",
            details={"principal_id": principal_id, "roles": roles, "is_contractor": is_contractor},
            conn=conn,
        )
        if owns:
            conn.commit()
    except Exception:
        if owns:
            conn.rollback()
        raise
    finally:
        if owns:
            conn.close()


def offboard(principal_id: str, *, reason: str, actor: str, conn=None) -> None:
    """Offboarding (ADR-22): desativa a identidade de console. Efeito em cascata:
    o principal é removido da resolução de approver (access_bundles.resolve_plan_approvers
    filtra `active = false`) e de steering (steering_resolution.is_steering_allowed).
    Idempotente. Grava audit (P8). `reason` obrigatório."""
    if not reason:
        raise ValueError("offboard exige `reason` (P8: nunca silencioso)")
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE dse_console_identity
                SET active = false, deactivated_at = now()
                WHERE principal_id = %s AND active = true
                """,
                (principal_id,),
            )
            changed = cur.rowcount
        emit(
            actor=actor, action="console_user_offboarded",
            tenant_id="platform",
            details={"principal_id": principal_id, "reason": reason, "was_active": changed > 0},
            conn=conn,
        )
        if owns:
            conn.commit()
    except Exception:
        if owns:
            conn.rollback()
        raise
    finally:
        if owns:
            conn.close()


def is_console_active(principal_id: str, conn=None) -> bool:
    """True sse o principal tem identidade de console ativa e não expirada.
    Um principal SEM linha em dse_console_identity retorna False aqui (não é um
    usuário de console) — mas note que `resolve_plan_approvers` trata CODEOWNERS
    sem linha como ativos; são checagens de propósitos diferentes."""
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT active, expires_at FROM dse_console_identity WHERE principal_id = %s
                """,
                (principal_id,),
            )
            row = cur.fetchone()
    finally:
        if owns:
            conn.close()
    if row is None:
        return False
    active, expires_at = row
    if not active:
        return False
    if expires_at is not None and _expired(expires_at):
        return False
    return True
