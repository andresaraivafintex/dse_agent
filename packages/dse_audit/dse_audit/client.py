"""Único caminho de escrita no audit ledger (WSF-E1-T1). Nenhum outro módulo do
monorepo deve executar `INSERT INTO audit_log` diretamente — importe `emit`.

Mínimo da fundação: grava usando a role `dse_app` (sem UPDATE/DELETE grant —
ver migrations/0001_foundation.sql). WS-F estende este pacote na Fase 1 com:
  - queries de reconstrução por WorkItem (`dse_audit.queries`)
  - export compliance-grade por tenant/período
  - garantir que a partição do tenant existe antes do primeiro insert
(ver services/platform/README.md para o que falta).
"""
from __future__ import annotations

import contextlib
import json
import os
from typing import Any

import psycopg2

_DSN = os.environ.get(
    "DSE_AUDIT_DATABASE_URL",
    os.environ.get("DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"),
)


def get_connection():
    return psycopg2.connect(_DSN)


def emit(
    *,
    actor: str,
    action: str,
    tenant_id: str,
    work_item_id: str | None = None,
    details: dict[str, Any] | None = None,
    conn=None,
) -> None:
    """Grava uma linha imutável no audit_log.

    `actor` é sempre um principal resolvido (via dse_identity) ou uma string
    `system:<component>` (ex.: `system:ingest-gateway`, `system:egress-proxy`)
    — nunca um platform_user_id bruto (P8: evidência precisa ser atribuível).

    Se `conn` for passado, participa da transação do caller (útil quando o
    evento de audit deve ser atômico com outra escrita); caso contrário abre e
    fecha sua própria conexão/transação.
    """
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit_log (work_item_id, tenant_id, actor, action, details)
                VALUES (%s, %s, %s, %s, %s::jsonb)
                """,
                (work_item_id, tenant_id, actor, action, json.dumps(details or {})),
            )
        if owns_conn:
            conn.commit()
    except Exception:
        if owns_conn:
            with contextlib.suppress(Exception):
                conn.rollback()
        raise
    finally:
        if owns_conn:
            conn.close()
