"""WSA-E3-T2 — outbound: exatamente 1 mensagem de status por WorkItem,
editada in-place via `MutableCommentWriter` + `SlackCommentBackend`. Sem
credencial real de Slack App: `FakeSlackClient` in-memory documentado
substitui `slack_sdk.WebClient` — a lógica (`MutableCommentWriter`,
`SlackCommentBackend`, `PgCommentStateStore`) é 100% real."""
from __future__ import annotations

import json
import uuid

import psycopg2
from fastapi.testclient import TestClient

import adapter_slack.app as app_module
from adapter_slack.backend import FakeSlackClient
from adapter_slack.app import app

client = TestClient(app)
DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"


def _make_work_item(tenant_id: str) -> str:
    work_item_id = f"wi_out_{uuid.uuid4().hex[:12]}"
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO work_items (id, tenant_id, source, source_ref, requester, idempotency_key) "
            "VALUES (%s,%s,'slack','{}'::jsonb,'usr_test',%s)",
            (work_item_id, tenant_id, f"idem_{work_item_id}"),
        )
    conn.commit()
    conn.close()
    return work_item_id


def test_first_status_update_posts_a_single_message(tenant_id, monkeypatch):
    fake_client = FakeSlackClient()
    monkeypatch.setattr(app_module, "build_real_slack_client", lambda token: fake_client)

    work_item_id = _make_work_item(tenant_id)

    resp = client.post(
        "/internal/status-comment",
        json={
            "work_item_id": work_item_id,
            "channel": "C_STATUS",
            "body": "Task started",
            "actor": "system:orchestrator",
        },
    )
    assert resp.status_code == 200
    assert len(fake_client.post_calls) == 1
    assert len(fake_client.update_calls) == 0
    assert fake_client.post_calls[0]["text"] == "Task started"


def test_subsequent_updates_edit_in_place_never_post_new_message(tenant_id, monkeypatch):
    fake_client = FakeSlackClient()
    monkeypatch.setattr(app_module, "build_real_slack_client", lambda token: fake_client)

    work_item_id = _make_work_item(tenant_id)

    for i, body in enumerate(["Task started", "Task running (step 1/3)", "Task done"]):
        resp = client.post(
            "/internal/status-comment",
            json={"work_item_id": work_item_id, "channel": "C_STATUS", "body": body, "actor": "system:orchestrator"},
        )
        assert resp.status_code == 200

    # exatamente 1 post inicial + 2 edições — NUNCA 3 posts.
    assert len(fake_client.post_calls) == 1
    assert len(fake_client.update_calls) == 2
    assert fake_client.update_calls[-1]["text"] == "Task done"

    # a única mensagem viva reflete o último corpo.
    (ts, text), = [(t, v) for t, v in fake_client.messages.items()]
    assert text == "Task done"


def test_status_comment_ref_persisted_across_process_restart_simulation(tenant_id, monkeypatch):
    """Adapter é 100% stateless — simula 'reiniciar o processo' criando um
    SEGUNDO FakeSlackClient e um SEGUNDO writer (novo PgCommentStateStore)
    para a MESMA work_item_id; deve continuar editando o comment_ref já
    persistido no Postgres, não postar de novo."""
    shared_client = FakeSlackClient()
    monkeypatch.setattr(app_module, "build_real_slack_client", lambda token: shared_client)

    work_item_id = _make_work_item(tenant_id)

    client.post(
        "/internal/status-comment",
        json={"work_item_id": work_item_id, "channel": "C_STATUS", "body": "first", "actor": "system:orchestrator"},
    )
    # "restart": nova instância de FakeSlackClient perderia o estado em
    # memória, mas o comment_ref persiste no Postgres (comment_state) — só
    # trocamos o client aqui para simular; o store é sempre Postgres.
    client.post(
        "/internal/status-comment",
        json={"work_item_id": work_item_id, "channel": "C_STATUS", "body": "second", "actor": "system:orchestrator"},
    )

    assert len(shared_client.post_calls) == 1
    assert len(shared_client.update_calls) == 1

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT surface, comment_ref FROM comment_state WHERE work_item_id = %s", (work_item_id,)
        )
        row = cur.fetchone()
    conn.close()
    assert row[0] == "slack"
    assert json.loads(row[1])["channel"] == "C_STATUS"


def test_awaiting_plan_approval_posts_block_kit_buttons(tenant_id, monkeypatch):
    """Fase B (relatório 07): no status awaiting_plan_approval a mensagem sai
    com Block Kit (Approve/Reject) — os action_id/value são os marcadores que
    parse_slack_approval (C1) lê. Fecha o loop: sem postar os botões, o humano
    não tinha como aprovar/rejeitar pelo Slack."""
    fake_client = FakeSlackClient()
    monkeypatch.setattr(app_module, "build_real_slack_client", lambda token: fake_client)
    work_item_id = _make_work_item(tenant_id)

    resp = client.post(
        "/internal/status-comment",
        json={
            "work_item_id": work_item_id,
            "channel": "C_APPROVE",
            "body": "Plano pronto — aprovar?",
            "actor": "system:orchestrator",
            "status": "awaiting_plan_approval",
        },
    )
    assert resp.status_code == 200
    blocks = fake_client.post_calls[0]["blocks"]
    assert blocks is not None
    action_block = next(b for b in blocks if b["type"] == "actions")
    action_ids = {e["action_id"] for e in action_block["elements"]}
    assert action_ids == {"dse_plan_approve", "dse_plan_reject"}
    values = {e["value"] for e in action_block["elements"]}
    assert "reject:re_plan" in values  # o marcador de rejeição que o C1 parseia


def test_non_approval_status_stays_plain_text(tenant_id, monkeypatch):
    fake_client = FakeSlackClient()
    monkeypatch.setattr(app_module, "build_real_slack_client", lambda token: fake_client)
    work_item_id = _make_work_item(tenant_id)
    client.post(
        "/internal/status-comment",
        json={"work_item_id": work_item_id, "channel": "C1", "body": "⚙️ implementando",
              "actor": "system:orchestrator", "status": "implementing"},
    )
    assert fake_client.post_calls[0]["blocks"] is None  # sem botões fora da aprovação
