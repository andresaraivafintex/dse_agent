from __future__ import annotations

import base64
import os

import psycopg2
import pytest

# Secret Base64 do outgoing webhook (mesma convenção do gateway/test_security).
_TEAMS_SECRET = base64.b64encode(b"teams_outgoing_webhook_secret_test").decode()
os.environ.setdefault("TEAMS_OUTGOING_SECRET", _TEAMS_SECRET)
os.environ["DSE_TENANT_ID"] = "test_tenant_teams_adapter"

DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
SUPERUSER_DSN = "postgresql://dse:dse_dev_only@localhost:5432/dse"


@pytest.fixture
def teams_secret_b64():
    return _TEAMS_SECRET


@pytest.fixture
def tenant_id():
    return os.environ["DSE_TENANT_ID"]


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    conn = psycopg2.connect(SUPERUSER_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM ingest_events WHERE work_item_id IN "
                "(SELECT id FROM work_items WHERE tenant_id LIKE 'test_tenant_%')"
            )
            cur.execute(
                "DELETE FROM comment_state WHERE work_item_id IN "
                "(SELECT id FROM work_items WHERE tenant_id LIKE 'test_tenant_%')"
            )
            cur.execute("DELETE FROM work_items WHERE tenant_id LIKE 'test_tenant_%'")
            cur.execute("DELETE FROM audit_log WHERE tenant_id LIKE 'test_tenant_%'")
        conn.commit()
    finally:
        conn.close()
