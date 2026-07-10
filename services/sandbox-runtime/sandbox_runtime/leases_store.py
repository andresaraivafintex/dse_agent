"""Persistência (best-effort) de `sandbox_leases` — bookkeeping durável do
lifecycle do sandbox, complementar aos labels do Docker (que são a fonte de
verdade de estado do container em si). Import de `psycopg2` é opcional: as
Activities continuam funcionando mesmo sem Postgres alcançável — só perdem
esta bookkeeping extra (o `dse_audit.emit` continua sendo o registro
obrigatório via P8, esta tabela é evidência adicional operacional)."""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("sandbox_runtime.leases_store")

try:
    import psycopg2
except Exception:  # pragma: no cover
    psycopg2 = None  # type: ignore[assignment]

_DSN = os.environ.get(
    "DSE_SANDBOX_DATABASE_URL",
    os.environ.get("DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"),
)


def _connect():
    if psycopg2 is None:
        return None
    try:
        return psycopg2.connect(_DSN)
    except Exception:  # noqa: BLE001
        logger.warning("sandbox_leases: Postgres indisponível, seguindo sem persistência extra")
        return None


def record_lifecycle_event(
    *, work_item_id: str, tenant_id: str, container_id: str | None, branch: str, resource_class: str, status: str
) -> None:
    conn = _connect()
    if conn is None:
        return
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sandbox_leases (work_item_id, tenant_id, container_id, branch, resource_class, status)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (work_item_id, tenant_id, container_id, branch, resource_class, status),
            )
    finally:
        conn.close()
