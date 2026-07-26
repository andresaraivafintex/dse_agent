"""Best-effort persistence of `sandbox_leases` — durable bookkeeping of the
sandbox lifecycle, complementary to the Docker labels (which are the source of
truth for the container's own state). The `psycopg2` import is optional: the
Activities keep working even with Postgres unreachable — they just lose this
extra bookkeeping (`dse_audit.emit` remains the mandatory record via P8; this
table is additional operational evidence)."""
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
        logger.warning("sandbox_leases: Postgres unavailable, continuing without extra persistence")
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
