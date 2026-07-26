"""Requires `make up && make migrate` to have run (real Postgres — the
append-only guarantee is structural, mocking it proves nothing)."""
import uuid

import psycopg2
import pytest
from dse_audit import emit, get_connection


@pytest.fixture()
def tenant():
    # unique tenant per test: dse_app has no DELETE (that is the whole point of
    # the immutability test), so no cleanup between tests is possible — isolating
    # by a unique tenant_id removes the need for any cleanup.
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
