"""WSA-E6-T2a — is_authorized_to_steer: fallback de allowlist, nunca
'qualquer um pode steerar'."""
from __future__ import annotations

import psycopg2

from ingest_gateway.steering import is_authorized_to_steer

DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"


def test_absent_row_means_unauthorized(tenant_id):
    assert is_authorized_to_steer(tenant_id, "usr_never_added") is False


def test_allowlisted_principal_is_authorized(tenant_id):
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenant_steering_allowlist (tenant_id, principal_id) VALUES (%s, %s)",
            (tenant_id, "usr_ops_lead"),
        )
    conn.commit()
    conn.close()

    assert is_authorized_to_steer(tenant_id, "usr_ops_lead") is True


def test_allowlist_is_scoped_per_tenant(tenant_id):
    other_tenant = tenant_id + "_other"
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenant_steering_allowlist (tenant_id, principal_id) VALUES (%s, %s)",
            (tenant_id, "usr_scoped"),
        )
    conn.commit()
    conn.close()

    assert is_authorized_to_steer(tenant_id, "usr_scoped") is True
    assert is_authorized_to_steer(other_tenant, "usr_scoped") is False

    # DELETE não é concedido a dse_app (ver migrations/0001_foundation.sql
    # design intent) — a fixture autouse `_cleanup_test_rows` do conftest já
    # varre por `tenant_id LIKE 'test_tenant_%'` usando o superuser `dse`,
    # o que cobre `other_tenant` também (mesmo prefixo). Nada a fazer aqui.
