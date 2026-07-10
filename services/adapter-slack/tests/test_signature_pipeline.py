"""WSA-E2-T1 no adapter: corpus de forgery contra o endpoint real
`/slack/events` — 100% deve ser rejeitado com 401 + audit row, nada
downstream (nenhum WorkItem) deve ser criado."""
from __future__ import annotations

import json
import time

import psycopg2
from fastapi.testclient import TestClient

from adapter_slack.app import app
from .helpers import sign

client = TestClient(app)
DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"


def _event_body():
    return json.dumps(
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "C_FORGERY",
                "ts": "1000.001",
                "user": "U_ATTACKER",
                "text": "please run something",
            },
        }
    ).encode()


def test_no_signature_headers_rejected():
    body = _event_body()
    resp = client.post("/slack/events", content=body, headers={})
    assert resp.status_code == 401


def test_wrong_signature_rejected():
    body = _event_body()
    ts = str(int(time.time()))
    resp = client.post(
        "/slack/events",
        content=body,
        headers={"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": "v0=deadbeef"},
    )
    assert resp.status_code == 401


def test_expired_timestamp_rejected():
    body = _event_body()
    old_ts = str(int(time.time()) - 3600)
    ts, sig = sign(body, timestamp=old_ts)
    resp = client.post(
        "/slack/events", content=body, headers={"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig}
    )
    assert resp.status_code == 401


def test_replay_of_captured_valid_request_rejected():
    body = _event_body()
    old_ts, sig = sign(body, timestamp=str(int(time.time()) - 900))
    resp = client.post(
        "/slack/events", content=body, headers={"X-Slack-Request-Timestamp": old_ts, "X-Slack-Signature": sig}
    )
    assert resp.status_code == 401


def test_forgery_corpus_creates_no_work_item_and_audits_rejection():
    body = _event_body()
    client.post("/slack/events", content=body, headers={})  # sem assinatura

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM work_items WHERE tenant_id = 'test_tenant_slack_adapter'")
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT count(*) FROM audit_log WHERE action = 'signature_rejected' AND tenant_id = 'test_tenant_slack_adapter'"
        )
        assert cur.fetchone()[0] >= 1
    conn.close()


def test_valid_signature_accepted_end_to_end():
    body = _event_body()
    ts, sig = sign(body)
    resp = client.post(
        "/slack/events", content=body, headers={"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["path"] == "new_task"
    assert "work_item_id" in data
