"""WSA-E6-T2a — Steering allowlist fallback.

`is_authorized_to_steer(tenant_id, principal_id)` decide se um principal
pode "steerar" (injetar direção nova via comentário/review_comment) uma
tarefa em andamento. Fase 1: fallback explícito de allowlist por tenant
(`tenant_steering_allowlist`, migrations/0002_wsa.sql). Ausência de linha =
NÃO autorizado — nunca "qualquer um pode steerar".

Assinatura estável por design: `(tenant_id, principal_id) -> bool`, sem
`conn` no contrato público, para o WS-F poder trocar a implementação por um
identity-map real (RBAC completo) na Fase 4 sem quebrar quem chama
(`ingest_gateway.correlate`, adapters). A implementação interna abre sua
própria conexão, seguindo a mesma convenção de `dse_identity.resolve_principal`.
"""
from __future__ import annotations

from .db import get_connection


def is_authorized_to_steer(tenant_id: str, principal_id: str) -> bool:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM tenant_steering_allowlist WHERE tenant_id = %s AND principal_id = %s",
                (tenant_id, principal_id),
            )
            return cur.fetchone() is not None
    finally:
        conn.close()
