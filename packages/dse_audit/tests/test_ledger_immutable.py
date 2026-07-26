"""Plano 08 §F (F3) — append-only ledger: the app role NEVER mutates audit_log
or model_call_ledger; not via UPDATE/DELETE (REVOKE + trigger) nor via TRUNCATE
(REVOKE). Proof of defense in depth: even WITH the grant, the trigger aborts.
Superuser keeps break-glass access (DR/retention/cleanup)."""
from __future__ import annotations

import uuid

import psycopg2
import pytest

APP_DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
SUPER_DSN = "postgresql://dse:dse_dev_only@localhost:5432/dse"


@pytest.fixture
def seeded_audit():
    tid = f"f3-{uuid.uuid4().hex[:8]}"
    # seed via the app role (dse_app DOES have INSERT — appending is allowed)
    app = psycopg2.connect(APP_DSN)
    with app.cursor() as cur:
        cur.execute(
            "INSERT INTO audit_log (work_item_id, tenant_id, actor, action, details) "
            "VALUES (NULL, %s, 'system:test', 'f3_seed', '{}'::jsonb)",
            (tid,),
        )
    app.commit()
    app.close()
    yield tid
    # cleanup via superuser (break-glass is allowed)
    sup = psycopg2.connect(SUPER_DSN)
    with sup.cursor() as cur:
        cur.execute("DELETE FROM audit_log WHERE tenant_id = %s", (tid,))
    sup.commit()
    sup.close()


def test_app_cannot_update_or_delete_or_truncate_audit_log(seeded_audit):
    app = psycopg2.connect(APP_DSN)
    for op in (
        "UPDATE audit_log SET actor='x' WHERE tenant_id=%s",
        "DELETE FROM audit_log WHERE tenant_id=%s",
    ):
        with pytest.raises(psycopg2.Error):
            with app.cursor() as cur:
                cur.execute(op, (seeded_audit,))
        app.rollback()
    with pytest.raises(psycopg2.Error):
        with app.cursor() as cur:
            cur.execute("TRUNCATE audit_log")
    app.rollback()
    app.close()


def test_trigger_blocks_even_with_grant(seeded_audit):
    """Defense in depth: if dse_app is ever granted UPDATE/DELETE by mistake, the
    TRIGGER still aborts (the REVOKE alone would not cover that case)."""
    sup = psycopg2.connect(SUPER_DSN)
    with sup.cursor() as cur:
        cur.execute("GRANT UPDATE, DELETE ON audit_log TO dse_app")
    sup.commit()
    try:
        app = psycopg2.connect(APP_DSN)
        with pytest.raises(psycopg2.Error) as exc:
            with app.cursor() as cur:
                cur.execute("UPDATE audit_log SET actor='hacker' WHERE tenant_id=%s", (seeded_audit,))
        assert "append-only" in str(exc.value)  # came from the trigger, not the REVOKE
        app.rollback()
        app.close()
    finally:
        with sup.cursor() as cur:
            cur.execute("REVOKE UPDATE, DELETE ON audit_log FROM dse_app")
        sup.commit()
        sup.close()


def test_app_cannot_mutate_model_call_ledger(seeded_audit):
    app = psycopg2.connect(APP_DSN)
    with pytest.raises(psycopg2.Error):
        with app.cursor() as cur:
            cur.execute("DELETE FROM model_call_ledger WHERE tenant_id='nope'")
    app.rollback()
    with pytest.raises(psycopg2.Error):
        with app.cursor() as cur:
            cur.execute("TRUNCATE model_call_ledger")
    app.rollback()
    app.close()
