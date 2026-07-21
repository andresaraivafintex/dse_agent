"""Outbound: exatamente 1 mensagem de status por WorkItem, editada in-place via
`MutableCommentWriter` + `TeamsCommentBackend`. Sem tenant Teams real:
`FakeTeamsClient` substitui o transporte Bot Framework — a lógica
(`MutableCommentWriter`, `TeamsCommentBackend`, `PgCommentStateStore`) é 100%
real. NÃO depende do enum `Platform.teams` (a surface é só a string "teams").
"""
from __future__ import annotations

import json
import uuid

import psycopg2
from fastapi.testclient import TestClient

import adapter_teams.app as app_module
from adapter_teams.app import app
from adapter_teams.backend import FakeTeamsClient

client = TestClient(app)
DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"

CONV = "19:channel_out@thread.tacv2"
SVC = "https://smba.trafficmanager.net/emea/"


def _make_work_item(tenant_id: str) -> str:
    work_item_id = f"wi_out_{uuid.uuid4().hex[:12]}"
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        # source='slack' aqui de propósito: o work_item é criado por outra
        # surface; o outbound Teams só posta o status (independe do source).
        cur.execute(
            "INSERT INTO work_items (id, tenant_id, source, source_ref, requester, idempotency_key) "
            "VALUES (%s,%s,'slack','{}'::jsonb,'usr_test',%s)",
            (work_item_id, tenant_id, f"idem_{work_item_id}"),
        )
    conn.commit()
    conn.close()
    return work_item_id


def _post(work_item_id: str, body: str):
    return client.post(
        "/internal/status-comment",
        json={
            "work_item_id": work_item_id,
            "conversation_id": CONV,
            "service_url": SVC,
            "body": body,
            "actor": "system:orchestrator",
        },
    )


def test_first_status_update_sends_single_activity(tenant_id, monkeypatch):
    fake = FakeTeamsClient()
    monkeypatch.setattr(app_module, "build_real_teams_client", lambda app_id, pw: fake)

    wi = _make_work_item(tenant_id)
    resp = _post(wi, "Task started")

    assert resp.status_code == 200
    assert len(fake.send_calls) == 1
    assert len(fake.update_calls) == 0
    assert fake.send_calls[0]["text"] == "Task started"
    assert fake.send_calls[0]["conversation_id"] == CONV


def test_subsequent_updates_edit_in_place_never_send_new(tenant_id, monkeypatch):
    fake = FakeTeamsClient()
    monkeypatch.setattr(app_module, "build_real_teams_client", lambda app_id, pw: fake)

    wi = _make_work_item(tenant_id)
    for body in ["Task started", "Task running (1/3)", "Task done"]:
        assert _post(wi, body).status_code == 200

    # exatamente 1 send inicial + 2 updates — NUNCA 3 sends.
    assert len(fake.send_calls) == 1
    assert len(fake.update_calls) == 2
    assert fake.update_calls[-1]["text"] == "Task done"
    (only_text,) = list(fake.activities.values())
    assert only_text == "Task done"


def test_status_ref_persisted_across_process_restart_simulation(tenant_id, monkeypatch):
    """Adapter 100% stateless: o comment_ref persiste no Postgres (comment_state),
    então trocar o client (simular restart) continua editando, não reenvia."""
    shared = FakeTeamsClient()
    monkeypatch.setattr(app_module, "build_real_teams_client", lambda app_id, pw: shared)

    wi = _make_work_item(tenant_id)
    _post(wi, "first")
    _post(wi, "second")

    assert len(shared.send_calls) == 1
    assert len(shared.update_calls) == 1

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT surface, comment_ref FROM comment_state WHERE work_item_id = %s", (wi,))
        row = cur.fetchone()
    conn.close()
    assert row[0] == "teams"
    ref = json.loads(row[1])
    assert ref["conversation_id"] == CONV
    assert ref["service_url"] == SVC
    assert "activity_id" in ref
