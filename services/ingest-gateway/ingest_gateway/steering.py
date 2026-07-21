"""WSA-E6-T2b — Steering policy sobre o identity map REAL.

`is_authorized_to_steer(tenant_id, principal_id)` decide se um principal pode
"steerar" (injetar direção nova via comentário/review_comment) uma tarefa em
andamento.

Histórico: a Fase 1 (WSA-E6-T2a) entregou o fallback de allowlist explícita
por tenant (`tenant_steering_allowlist`, migrations/0002_wsa.sql) — "ausência
de linha = não autorizado", nunca "qualquer um pode steerar". A ASSINATURA
`(tenant_id, principal_id) -> bool` foi mantida estável de propósito, para que
a Fase 4 pudesse trocar a resolução por baixo sem quebrar quem chama
(`ingest_gateway.correlate`, adapters). Esta é essa troca.

A Fase 4 substitui a resolução por *identity-map-backed*: em vez de só checar
pertencimento a uma allowlist, resolvemos se o principal tem o **papel** certo
para dirigir a tarefa daquele tenant, usando as fontes da verdade que o WS-F
já entregou na Fase 2:

  1. `dse_console_identity` (WSF-E3-T3 / ADR-22) — a identidade de console do
     principal, com `roles` (RBAC) e o estado de offboarding (`active`,
     `expires_at`). Um papel em `STEERING_ROLES` autoriza; um principal
     offboardado/expirado é NEGADO mesmo que continue em qualquer lista
     (o offboarding é a autoridade que sobrepõe — WSF-E3-T3).
  2. `dse_access_bundle` (WSF-E3-T2) — os `designated_approvers` do bundle
     efetivo do tenant são, por construção, os humanos confiados a
     aprovar/dirigir aquele tenant; estar na lista é o "papel de approver"
     resolvido via bundle.

Fallback DOCUMENTADO (enquanto o identity map não resolve um papel): a
allowlist explícita da Fase 1 (`tenant_steering_allowlist`), que na prática é
semeada com o requester + os CODEOWNERS-equivalentes de confiança do tenant.
Isto preserva o comportamento e os testes da Fase 1 exatamente, e cobre os
tenants que ainda não têm console identity / access bundle configurados.

Invariantes preservadas (Fase 1):
  - **Nega por default (P1/deny-by-default)**: nenhuma das fontes resolvendo
    um papel/pertencimento => `False`. Nunca "qualquer um pode steerar".
  - **Assinatura estável**: `(tenant_id, principal_id) -> bool`, sem `conn` no
    contrato público (abre a própria conexão, como `resolve_principal`).
  - **Não-autorizado não sinaliza + audit**: a linha de audit de rejeição
    (`steering_rejected_unauthorized`) e o "não vira signal" continuam sendo
    responsabilidade de `ingest_gateway.correlate` (invariante da Fase 1) —
    este módulo só devolve o booleano. No caminho POSITIVO emitimos uma linha
    de proveniência (`steering_authorized`, com o MÉTODO de resolução) para
    dar evidência (P8) de COMO a autorização foi concedida.

Tudo aqui é comparação determinística de conjuntos/strings (P1) — nenhum LLM
decide quem pode steerar.
"""
from __future__ import annotations

import json
import logging

from dse_audit import emit as audit_emit

from .db import get_connection

logger = logging.getLogger("ingest_gateway.steering")

# Papéis (em dse_console_identity.roles) que autorizam steering de uma tarefa.
# Conjunto conservador (P7 boring-first): os humanos que operam/aprovam/mantêm.
# WS-F administra a atribuição desses papéis no console (WSF-E3-T3); aqui só
# comparamos conjuntos.
STEERING_ROLES: frozenset[str] = frozenset(
    {"operator", "approver", "steerer", "maintainer", "platform_admin", "admin"}
)

# Métodos de resolução (gravados no audit de proveniência — P8).
_METHOD_ROLE_CONSOLE = "role_console"      # papel RBAC resolvido via dse_console_identity
_METHOD_ROLE_BUNDLE = "role_bundle"        # designated_approver do access bundle do tenant
_METHOD_ALLOWLIST = "allowlist_fallback"   # fallback documentado (requester + CODEOWNERS seed)


def is_authorized_to_steer(tenant_id: str, principal_id: str) -> bool:
    """True sse o principal tem papel para dirigir uma tarefa deste tenant.

    Ordem de resolução (determinística; nega por default):
      0. Offboarding é autoridade que sobrepõe — principal com linha de console
         inativa/expirada é negado, mesmo que continue em allowlist/bundle.
      1. Papel RBAC via console identity (escopado ao tenant: home tenant do
         principal == tenant OU principal é operador de plataforma tenant NULL).
      2. Papel de approver via access bundle efetivo do tenant.
      3. Fallback documentado: allowlist explícita da Fase 1.
    """
    if not tenant_id or not principal_id:
        return False

    conn = get_connection()
    try:
        console = _load_console_identity(conn, principal_id)

        # 0. Offboarding sobrepõe tudo (WSF-E3-T3): linha existe e desativada/
        #    expirada => negado, independentemente de allowlist/bundle.
        if console is not None and not console["effective_active"]:
            return False

        method = _resolve_method(conn, tenant_id, principal_id, console)
        if method is None:
            return False

        # P8: proveniência do GRANT (a rejeição continua sendo auditada por
        # correlate, invariante da Fase 1). Best-effort — nunca deixa uma falha
        # de audit derrubar a autorização de um steer legítimo, mas loga.
        try:
            audit_emit(
                actor=principal_id,
                action="steering_authorized",
                tenant_id=tenant_id,
                details={"method": method},
            )
        except Exception:  # noqa: BLE001 — audit best-effort no caminho de grant
            logger.warning(
                "falha ao emitir audit steering_authorized (tenant=%s principal=%s method=%s)",
                tenant_id, principal_id, method, exc_info=True,
            )
        return True
    finally:
        conn.close()


def _resolve_method(conn, tenant_id: str, principal_id: str, console: dict | None) -> str | None:
    """Retorna o método que autoriza, ou None se nenhuma fonte resolve um papel."""
    # 1. Papel RBAC via console identity, escopado ao tenant.
    if console is not None and (set(console["roles"]) & STEERING_ROLES):
        home = console["tenant_id"]
        if home is None or home == tenant_id:
            return _METHOD_ROLE_CONSOLE

    # 2. Papel de approver via access bundle efetivo do tenant (channel default).
    if _is_designated_approver(conn, tenant_id, principal_id):
        return _METHOD_ROLE_BUNDLE

    # 3. Fallback documentado: allowlist explícita da Fase 1.
    if _in_steering_allowlist(conn, tenant_id, principal_id):
        return _METHOD_ALLOWLIST

    return None


def _load_console_identity(conn, principal_id: str) -> dict | None:
    """Lê a identidade de console do principal, se existir. `effective_active`
    já combina `active` com a expiração de contractor (ADR-22). `roles` é
    sempre uma lista (JSONB), `tenant_id` é o home tenant (None = operador de
    plataforma multi-tenant)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT roles,
                   tenant_id,
                   (active AND (expires_at IS NULL OR expires_at > now())) AS effective_active
            FROM dse_console_identity
            WHERE principal_id = %s
            """,
            (principal_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    roles, home_tenant, effective_active = row
    return {
        "roles": list(roles or []),
        "tenant_id": home_tenant,
        "effective_active": bool(effective_active),
    }


def _is_designated_approver(conn, tenant_id: str, principal_id: str) -> bool:
    """True sse o principal está nos `designated_approvers` do access bundle
    EFETIVO do tenant (channel default = NULL). Resolução mínima e determinística
    do bundle default habilitado — o steering não carrega um `channel` no
    contrato estável, então usamos o default do tenant (mesma granularidade da
    allowlist da Fase 1). Um bundle desabilitado não concede."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM dse_access_bundle
            WHERE tenant_id = %s
              AND channel IS NULL
              AND enabled = true
              AND designated_approvers @> %s::jsonb
            """,
            (tenant_id, json.dumps([principal_id])),
        )
        return cur.fetchone() is not None


def _in_steering_allowlist(conn, tenant_id: str, principal_id: str) -> bool:
    """Fallback da Fase 1: pertencimento à `tenant_steering_allowlist`
    (tenant-scoped). Ausência de linha = não autorizado por esta via."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM tenant_steering_allowlist WHERE tenant_id = %s AND principal_id = %s",
            (tenant_id, principal_id),
        )
        return cur.fetchone() is not None
