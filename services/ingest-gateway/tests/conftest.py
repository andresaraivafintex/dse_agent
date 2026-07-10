from __future__ import annotations

import os
import uuid

import psycopg2
import pytest

DSN = os.environ.get(
    "DSE_TEST_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
)


@pytest.fixture
def db_conn():
    conn = psycopg2.connect(DSN)
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture
def tenant_id():
    return f"test_tenant_{uuid.uuid4().hex[:8]}"


_SUPERUSER_DSN = os.environ.get(
    "DSE_TEST_SUPERUSER_DATABASE_URL", "postgresql://dse:dse_dev_only@localhost:5432/dse"
)


@pytest.fixture(autouse=True)
def _cleanup_test_rows():
    """Limpa linhas criadas pelos testes (identificadas pelo prefixo
    test_tenant_/wi_ + o padrão de event_id determinístico dos testes) para
    não acumular lixo entre execuções repetidas contra o Postgres real.

    Usa o superuser `dse` (não `dse_app`) porque DELETE em work_items/
    ingest_events é deliberadamente NÃO concedido a `dse_app` em produção
    (ver migrations/0001_foundation.sql) — limpeza de teste é uma
    preocupação de dev-only, não deve ampliar o grant de produção."""
    yield
    conn = psycopg2.connect(_SUPERUSER_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ingest_events WHERE work_item_id IN (SELECT id FROM work_items WHERE tenant_id LIKE 'test_tenant_%')")
            cur.execute("DELETE FROM comment_state WHERE work_item_id IN (SELECT id FROM work_items WHERE tenant_id LIKE 'test_tenant_%')")
            cur.execute("DELETE FROM work_items WHERE tenant_id LIKE 'test_tenant_%'")
            cur.execute("DELETE FROM channel_kill_switches WHERE tenant_id LIKE 'test_tenant_%'")
            cur.execute("DELETE FROM tenant_steering_allowlist WHERE tenant_id LIKE 'test_tenant_%'")
            cur.execute("DELETE FROM audit_log WHERE tenant_id LIKE 'test_tenant_%'")
        conn.commit()
    finally:
        conn.close()
