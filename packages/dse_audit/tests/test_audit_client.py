"""Requer `make up && make migrate` rodando (Postgres real — a garantia
append-only é estrutural, não vale mockar)."""
import uuid

import psycopg2
import pytest
from dse_audit import emit, get_connection


@pytest.fixture()
def tenant():
    # tenant único por teste: dse_app não tem DELETE (é o ponto do teste de
    # imutabilidade), então não há limpeza possível entre testes — isolar por
    # tenant_id único evita qualquer necessidade de cleanup.
    return f"acme-test-{uuid.uuid4().hex[:8]}"


def test_emit_writes_a_row(tenant):
    emit(actor="system:test", action="unit_test_action", tenant_id=tenant, details={"k": "v"})

    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT actor, action, details FROM audit_log WHERE tenant_id = %s", (tenant,)
        )
        row = cur.fetchone()
    conn.close()
    assert row is not None
    actor, action, details = row
    assert actor == "system:test"
    assert action == "unit_test_action"
    assert details == {"k": "v"}


def test_audit_log_rejects_update_and_delete(tenant):
    emit(actor="system:test", action="immutability_probe", tenant_id=tenant)

    conn = get_connection()
    conn.autocommit = False
    try:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE audit_log SET action = 'tampered' WHERE tenant_id = %s", (tenant,)
                )
    finally:
        conn.rollback()

    conn2 = get_connection()
    try:
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            with conn2.cursor() as cur:
                cur.execute("DELETE FROM audit_log WHERE tenant_id = %s", (tenant,))
    finally:
        conn2.rollback()
        conn2.close()
    conn.close()
