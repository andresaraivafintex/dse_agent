"""Plano 08 §F (F3) — ledger append-only: a role de app NUNCA muta audit_log
nem model_call_ledger; nem por UPDATE/DELETE (REVOKE + trigger) nem por
TRUNCATE (REVOKE). Prova de defesa em profundidade: mesmo COM o grant, o
trigger aborta. Superuser tem break-glass (DR/retenção/limpeza)."""
from __future__ import annotations

import uuid

import psycopg2
import pytest

APP_DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
SUPER_DSN = "postgresql://dse:dse_dev_only@localhost:5432/dse"


@pytest.fixture
def seeded_audit():
    tid = f"f3-{uuid.uuid4().hex[:8]}"
    # semeia via app (dse_app TEM INSERT — append é permitido)
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
    # limpeza via superuser (break-glass permitido)
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
    """Defesa em profundidade: se dse_app receber UPDATE/DELETE por engano, o
    TRIGGER ainda aborta (o REVOKE sozinho não cobriria esse caso)."""
    sup = psycopg2.connect(SUPER_DSN)
    with sup.cursor() as cur:
        cur.execute("GRANT UPDATE, DELETE ON audit_log TO dse_app")
    sup.commit()
    try:
        app = psycopg2.connect(APP_DSN)
        with pytest.raises(psycopg2.Error) as exc:
            with app.cursor() as cur:
                cur.execute("UPDATE audit_log SET actor='hacker' WHERE tenant_id=%s", (seeded_audit,))
        assert "append-only" in str(exc.value)  # veio do trigger, não do REVOKE
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
