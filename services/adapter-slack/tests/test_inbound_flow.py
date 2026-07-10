"""WSA-E3-T1 — fluxo inbound completo: app_mention cria task_request,
reply em thread existente correlaciona via thread_ts (signal), interação de
botão vira kind=approval, TOCTOU snapshot (WSA-E2-T2) e sanitização
(WSA-E2-T3) fim-a-fim."""
from __future__ import annotations

import json

import psycopg2
from fastapi.testclient import TestClient

from adapter_slack.app import app
from .helpers import sign

client = TestClient(app)
DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"


def _post_event(event: dict) -> dict:
    body = json.dumps({"type": "event_callback", "event": event}).encode()
    ts, sig = sign(body)
    resp = client.post(
        "/slack/events", content=body, headers={"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig}
    )
    assert resp.status_code == 200
    return resp.json()


def _post_interaction(payload: dict) -> dict:
    body = f"payload={json.dumps(payload)}".encode()
    ts, sig = sign(body)
    resp = client.post(
        "/slack/interactions",
        content=body,
        headers={
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": sig,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    assert resp.status_code == 200
    return resp.json()


def test_app_mention_creates_new_task_work_item():
    data = _post_event(
        {
            "type": "app_mention",
            "channel": "C_ABC",
            "ts": "5000.001",
            "user": "U_REQUESTER",
            "text": "fix the login bug please",
        }
    )
    assert data["path"] == "new_task"

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT status, source FROM work_items WHERE id = %s", (data["work_item_id"],))
        row = cur.fetchone()
        assert row == ("new", "slack")
    conn.close()


def test_reply_in_existing_thread_correlates_to_signal_not_new_task():
    created = _post_event(
        {
            "type": "app_mention",
            "channel": "C_THREAD1",
            "ts": "6000.001",
            "user": "U_REQUESTER",
            "text": "please add a new endpoint",
        }
    )
    assert created["path"] == "new_task"
    original_work_item_id = created["work_item_id"]

    reply = _post_event(
        {
            "type": "message",
            "channel": "C_THREAD1",
            "ts": "6000.050",
            "thread_ts": "6000.001",
            "user": "U_REQUESTER",
            "text": "actually make it a POST endpoint",
        }
    )

    assert reply["path"] == "signal"
    assert reply["work_item_id"] == original_work_item_id

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM work_items WHERE tenant_id = 'test_tenant_slack_adapter'")
        # apenas 1 work_item criado nesse fluxo (a reply não cria um segundo)
        cur.execute(
            "SELECT count(*) FROM work_items WHERE tenant_id = 'test_tenant_slack_adapter' "
            "AND source_ref @> %s::jsonb",
            (json.dumps({"channel": "C_THREAD1", "thread_ts": "6000.001"}),),
        )
        assert cur.fetchone()[0] == 1

        cur.execute(
            "SELECT kind FROM ingest_events WHERE work_item_id = %s ORDER BY id",
            (original_work_item_id,),
        )
        kinds = [r[0] for r in cur.fetchall()]
        assert kinds == ["task_request", "clarification_answer"]
    conn.close()


def test_toctou_snapshot_freezes_content_at_event_time():
    """O snapshot gravado é exatamente o texto que veio no webhook — provamos
    que não há re-fetch: mesmo simulando uma 'edição' via um segundo webhook
    com texto diferente para a MESMA mensagem (mesmo ts), o primeiro
    ingest_event já persistido continua com o conteúdo original."""
    original_text = "original instruction: implement feature X"
    data = _post_event(
        {
            "type": "app_mention",
            "channel": "C_TOCTOU",
            "ts": "7000.001",
            "user": "U_REQUESTER",
            "text": original_text,
        }
    )
    work_item_id = data["work_item_id"]

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT payload FROM ingest_events WHERE work_item_id = %s", (work_item_id,))
        payload = cur.fetchone()[0]
    conn.close()

    assert payload["content_snapshot"] == original_text

    # Reentrega do MESMO evento (mesmo channel+ts) com texto "editado" —
    # como platform+thread+message (ts) são idênticos, o event_id é IGUAL e
    # a linha é deduplicada (ON CONFLICT DO NOTHING) — o snapshot já gravado
    # NUNCA é sobrescrito por uma versão "editada".
    edited_data = _post_event(
        {
            "type": "app_mention",
            "channel": "C_TOCTOU",
            "ts": "7000.001",
            "user": "U_REQUESTER",
            "text": "EDITED: implement feature Y instead",
        }
    )
    assert edited_data["work_item_id"] == work_item_id

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT payload FROM ingest_events WHERE work_item_id = %s ORDER BY id", (work_item_id,)
        )
        rows = cur.fetchall()
    conn.close()

    assert len(rows) == 1  # dedup — nenhuma segunda linha
    assert rows[0][0]["content_snapshot"] == original_text  # não foi sobrescrito


def test_sanitize_pipeline_redacts_secret_before_reaching_payload_sanitized_field():
    data = _post_event(
        {
            "type": "app_mention",
            "channel": "C_SECRET",
            "ts": "8000.001",
            "user": "U_REQUESTER",
            "text": "here is my token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 use it to deploy",
        }
    )
    work_item_id = data["work_item_id"]

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT payload FROM ingest_events WHERE work_item_id = %s", (work_item_id,))
        payload = cur.fetchone()[0]
    conn.close()

    # snapshot original intacto (auditoria) — o secret aparece aqui de propósito.
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" in payload["content_snapshot"]
    # versão sanitizada (a que segue no pipeline) tem o secret redigido.
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in payload["sanitized_content"]
    assert "[REDACTED:github_token]" in payload["sanitized_content"]


def test_block_action_button_click_correlates_as_approval_signal():
    created = _post_event(
        {
            "type": "app_mention",
            "channel": "C_APPROVE",
            "ts": "9000.001",
            "user": "U_REQUESTER",
            "text": "please deploy this",
        }
    )
    work_item_id = created["work_item_id"]

    interaction_payload = {
        "type": "block_actions",
        "channel": {"id": "C_APPROVE"},
        "message": {"ts": "9000.500", "thread_ts": "9000.001"},
        "user": {"id": "U_REQUESTER"},
        "action_ts": "9000.600",
        "actions": [{"action_id": "approve_pr", "value": "approved"}],
    }
    data = _post_interaction(interaction_payload)

    assert data["path"] == "signal"
    assert data["work_item_id"] == work_item_id

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT kind, payload FROM ingest_events WHERE work_item_id = %s ORDER BY id",
            (work_item_id,),
        )
        rows = cur.fetchall()
    conn.close()

    kinds = [r[0] for r in rows]
    assert kinds == ["task_request", "approval"]
    assert "button:approve_pr=approved" in rows[1][1]["content_snapshot"]
