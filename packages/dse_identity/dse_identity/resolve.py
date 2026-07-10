"""Fundações do identity map (WSF-E3-T1). Fase 1: resolução por auto-registro
na primeira aparição — SEM SSO/SCIM (isso é ADR-22 fechado + WSF-E3-T3,
Fase 2). Todo adapter chama `resolve_principal` antes de gravar `actor` em
qualquer `ConversationEvent`, `WorkItem.requester` ou linha de audit.

Contrato estável (WS-A depende disto na Fase 1; WS-F troca a implementação
por baixo na Fase 2 sem quebrar a assinatura — ver WSA-E6-T2b).
"""
from __future__ import annotations

import os
import uuid

import psycopg2

_DSN = os.environ.get(
    "DSE_IDENTITY_DATABASE_URL",
    os.environ.get("DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"),
)


def resolve_principal(platform: str, platform_user_id: str, display_name: str | None = None) -> str:
    """Retorna o principal_id único para (platform, platform_user_id),
    criando-o na primeira aparição. Idempotente: chamadas repetidas com o
    mesmo par platform/platform_user_id sempre retornam o mesmo principal_id.
    """
    conn = psycopg2.connect(_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT principal_id FROM identity_links WHERE platform = %s AND platform_user_id = %s",
                (platform, platform_user_id),
            )
            row = cur.fetchone()
            if row is not None:
                return row[0]

            principal_id = f"usr_{uuid.uuid4().hex[:16]}"
            cur.execute(
                "INSERT INTO principals (id, display_name) VALUES (%s, %s)",
                (principal_id, display_name),
            )
            cur.execute(
                "INSERT INTO identity_links (principal_id, platform, platform_user_id) "
                "VALUES (%s, %s, %s) ON CONFLICT (platform, platform_user_id) DO NOTHING",
                (principal_id, platform, platform_user_id),
            )
        conn.commit()

        # corrida rara: outro processo criou entre o SELECT e o INSERT — a
        # constraint ON CONFLICT acima não sobrescreve; relê para pegar o
        # principal_id vencedor (garante 1 principal por platform_user_id).
        with conn.cursor() as cur:
            cur.execute(
                "SELECT principal_id FROM identity_links WHERE platform = %s AND platform_user_id = %s",
                (platform, platform_user_id),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()
