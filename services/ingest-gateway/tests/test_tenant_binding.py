"""WSA-E1-T5 — resolução plataforma -> tenant contra Postgres real.

Binding presente resolve; binding ausente cai no DSE_TENANT_ID default COM
audit row de aviso (nunca adivinha)."""
from __future__ import annotations

import os

import psycopg2
import pytest

from ingest_gateway import resolve_tenant

DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"


def test_binding_present_resolves_to_mapped_tenant(db_conn):
    tenant = "test_tenant_bound_xyz"
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenant_platform_bindings (platform, binding_key, tenant_id) VALUES (%s,%s,%s) "
            "ON CONFLICT (platform, binding_key) DO UPDATE SET tenant_id = EXCLUDED.tenant_id",
            ("slack", "T_TEST_WS", tenant),
        )
    db_conn.commit()

    rt = resolve_tenant(db_conn, platform="slack", binding_key="T_TEST_WS")
    db_conn.commit()
    assert rt.tenant_id == tenant
    assert rt.from_binding is True


def test_missing_binding_falls_back_to_default_with_audit(db_conn, monkeypatch):
    monkeypatch.setenv("DSE_TENANT_ID", "test_tenant_default_fallback")

    rt = resolve_tenant(db_conn, platform="github", binding_key="99999_unmapped")
    db_conn.commit()

    assert rt.tenant_id == "test_tenant_default_fallback"
    assert rt.from_binding is False

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM audit_log WHERE tenant_id=%s AND action='tenant_binding_missing_fallback_default'",
            ("test_tenant_default_fallback",),
        )
        assert cur.fetchone()[0] >= 1
    conn.close()


def test_none_binding_key_falls_back_without_lookup(db_conn, monkeypatch):
    monkeypatch.setenv("DSE_TENANT_ID", "test_tenant_default_fallback2")
    rt = resolve_tenant(db_conn, platform="jira", binding_key=None)
    db_conn.commit()
    assert rt.tenant_id == "test_tenant_default_fallback2"
    assert rt.from_binding is False
