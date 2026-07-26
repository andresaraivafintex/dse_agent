"""Inbound `/teams/messages`: real defense #1 (outgoing webhook HMAC signature)
+ activation guard. The forgery corpus must be 401 and write nothing; a validly
signed request passes the defense and (while the foundation does not expose
`Platform.teams`) returns 501 `teams_not_activated` BEFORE any write.
"""
from __future__ import annotations

import psycopg2
from fastapi.testclient import TestClient

from adapter_teams.app import app
from adapter_teams.platform_compat import is_activated
from .helpers import activity_body, teams_auth_header

client = TestClient(app)
DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
TENANT = "test_tenant_teams_adapter"


def _count(cur, sql, params=()):
    cur.execute(sql, params)
    return cur.fetchone()[0]


def test_no_authorization_header_rejected():
    resp = client.post("/teams/messages", content=activity_body(), headers={})
    assert resp.status_code == 401


def test_wrong_signature_rejected():
    resp = client.post(
        "/teams/messages", content=activity_body(), headers={"Authorization": "HMAC deadbeef"}
    )
    assert resp.status_code == 401


def test_tampered_body_rejected(teams_secret_b64):
    original = activity_body(text="<at>DSE Bot</at> approve low-risk change")
    auth = teams_auth_header(teams_secret_b64, original)
    tampered = activity_body(text="<at>DSE Bot</at> approve high-risk change")
    resp = client.post("/teams/messages", content=tampered, headers={"Authorization": auth})
    assert resp.status_code == 401


def test_forgery_corpus_creates_no_work_item_and_audits_rejection():
    client.post("/teams/messages", content=activity_body(), headers={})  # no signature

    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            assert _count(cur, "SELECT count(*) FROM work_items WHERE tenant_id = %s", (TENANT,)) == 0
            assert (
                _count(
                    cur,
                    "SELECT count(*) FROM audit_log WHERE action='signature_rejected' AND tenant_id=%s",
                    (TENANT,),
                )
                >= 1
            )
    finally:
        conn.close()


def test_valid_signature_passes_defense_but_gated_on_activation(teams_secret_b64):
    """Valid signature -> passes defense #1. While Teams is not activated, the
    endpoint returns 501 teams_not_activated (clean failure, P6) WITHOUT creating
    a WorkItem, and leaves a `teams_inbound_not_activated` audit row (P8)."""
    body = activity_body(text="<at>DSE Bot</at> please fix the login bug")
    auth = teams_auth_header(teams_secret_b64, body)
    resp = client.post("/teams/messages", content=body, headers={"Authorization": auth})

    if is_activated():
        # Post-activation: the full pipeline runs (the new expected behavior).
        assert resp.status_code == 200
        assert resp.json()["path"] in {"new_task", "signal", "unauthorized", "blocked_kill_switch"}
    else:
        assert resp.status_code == 501
        assert "not enabled" in resp.json()["error"]
        conn = psycopg2.connect(DSN)
        try:
            with conn.cursor() as cur:
                assert _count(cur, "SELECT count(*) FROM work_items WHERE tenant_id=%s", (TENANT,)) == 0
                assert (
                    _count(
                        cur,
                        "SELECT count(*) FROM audit_log WHERE action='teams_inbound_not_activated' AND tenant_id=%s",
                        (TENANT,),
                    )
                    >= 1
                )
        finally:
            conn.close()


def test_non_message_activity_ignored(teams_secret_b64):
    import json as _json

    body = _json.dumps({"type": "conversationUpdate", "id": "x"}).encode()
    auth = teams_auth_header(teams_secret_b64, body)
    resp = client.post("/teams/messages", content=body, headers={"Authorization": auth})
    assert resp.status_code == 200
    assert resp.json()["path"] == "ignored_non_message"
