"""Persistência (best-effort) de `egress_credential_leases` — evidência
durável e consultável do SLO de revogação de 60s (WSC-E2-T2), além do que já
vai para `audit_log` via `dse_audit`. Import de `psycopg2` é opcional: o
proxy continua funcionando (allowlist/deny/injeção) mesmo sem Postgres
alcançável — só perde esta bookkeeping extra, nunca quebra o path principal.
"""
from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger("egress_proxy.leases_store")

try:
    import psycopg2
except Exception:  # pragma: no cover
    psycopg2 = None  # type: ignore[assignment]

_DSN = os.environ.get(
    "DSE_EGRESS_DATABASE_URL",
    os.environ.get("DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"),
)


def _connect():
    if psycopg2 is None:
        return None
    try:
        return psycopg2.connect(_DSN)
    except Exception:  # noqa: BLE001
        logger.warning("egress_credential_leases: Postgres indisponível, seguindo sem persistência extra")
        return None


def record_issued(*, credential_id: str, work_item_id: str, tenant_id: str, repo: str, branch: str, allowed_actions, fixture: bool) -> None:
    conn = _connect()
    if conn is None:
        return
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO egress_credential_leases
                    (credential_id, work_item_id, tenant_id, repo, branch, allowed_actions, fixture)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (credential_id) DO NOTHING
                """,
                (credential_id, work_item_id, tenant_id, repo, branch, json.dumps(sorted(allowed_actions)), fixture),
            )
    finally:
        conn.close()


def record_revoked(*, credential_id: str, latency_s: float) -> None:
    conn = _connect()
    if conn is None:
        return
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE egress_credential_leases
                   SET revoked_at = now(), revoke_latency_s = %s
                 WHERE credential_id = %s
                """,
                (latency_s, credential_id),
            )
    finally:
        conn.close()
