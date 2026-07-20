"""WSF-E3-T3 (parte do offboarding) — resolução de "quem pode steerar uma
tarefa", combinando a allowlist explícita do WS-A (`tenant_steering_allowlist`)
com o estado de offboarding do console (`dse_console_identity.active`).

O WS-A (WSA-E6-T2a) já tem a allowlist como fonte da verdade de autorização de
steering ("ausência de linha = não autorizado"). Este helper adiciona a camada
de offboarding do ADR-22: um principal offboardado (`active = false`) NÃO pode
steerar mesmo que ainda tenha linha na allowlist — o offboarding é a autoridade
que sobrepõe. Puramente determinístico (P1); nega por default.
"""
from __future__ import annotations

import os

import psycopg2

from .sso import is_console_active

_DSN = os.environ.get(
    "DSE_PLATFORM_DATABASE_URL",
    os.environ.get("DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"),
)


def _get_connection():
    return psycopg2.connect(_DSN)


def is_steering_allowed(tenant_id: str, principal_id: str, conn=None) -> bool:
    """True sse o principal está na allowlist de steering do tenant E não está
    offboardado no console. Um principal que nunca logou no console (sem linha
    em dse_console_identity) NÃO é bloqueado por isso — só é bloqueado se
    EXPLICITAMENTE desativado/expirado (mesma regra de
    access_bundles.resolve_plan_approvers). Nega por default."""
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM tenant_steering_allowlist WHERE tenant_id = %s AND principal_id = %s",
                (tenant_id, principal_id),
            )
            in_allowlist = cur.fetchone() is not None
        if not in_allowlist:
            return False
        # tem linha em console_identity e está desativado? bloqueia.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM dse_console_identity WHERE principal_id = %s",
                (principal_id,),
            )
            has_console = cur.fetchone() is not None
        if has_console and not is_console_active(principal_id, conn=conn):
            return False
        return True
    finally:
        if owns:
            conn.close()
