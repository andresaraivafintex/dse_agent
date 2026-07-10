"""WSA-E2-T1 no adapter GitHub: corpus de forgery — 100% rejeitado com 401
+ audit row, nada downstream criado."""
from __future__ import annotations

import json

import psycopg2
from fastapi.testclient import TestClient

from adapter_github.app import app
from .helpers import sign

client = TestClient(app)
DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"


def _body():
    return json.dumps(
        {
            "action": "labeled",
            "issue": {"number": 1, "title": "t", "body": "b"},
            "label": {"name": "dse"},
            "repository": {"full_name": "acme/widgets"},
            "sender": {"login": "attacker"},
        }
    ).encode()


def test_no_signature_rejected():
    resp = client.post(
        "/github/webhook", content=_body(), headers={"X-GitHub-Event": "issues", "X-GitHub-Delivery": "d1"}
    )
    assert resp.status_code == 401


def test_wrong_signature_rejected():
    resp = client.post(
        "/github/webhook",
        content=_body(),
        headers={"X-GitHub-Event": "issues", "X-GitHub-Delivery": "d2", "X-Hub-Signature-256": "sha256=deadbeef"},
    )
    assert resp.status_code == 401


def test_tampered_body_after_signing_rejected():
    body = _body()
    sig = sign(body)
    tampered = body.replace(b"labeled", b"opened")
    resp = client.post(
        "/github/webhook",
        content=tampered,
        headers={"X-GitHub-Event": "issues", "X-GitHub-Delivery": "d3", "X-Hub-Signature-256": sig},
    )
    assert resp.status_code == 401


def test_malformed_signature_header_rejected():
    body = _body()
    resp = client.post(
        "/github/webhook",
        content=body,
        headers={"X-GitHub-Event": "issues", "X-GitHub-Delivery": "d4", "X-Hub-Signature-256": "not-valid"},
    )
    assert resp.status_code == 401


def test_forgery_corpus_creates_no_work_item_and_audits():
    client.post("/github/webhook", content=_body(), headers={"X-GitHub-Event": "issues", "X-GitHub-Delivery": "d5"})

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM work_items WHERE tenant_id = 'test_tenant_github_adapter'")
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT count(*) FROM audit_log WHERE action='signature_rejected' AND tenant_id='test_tenant_github_adapter'"
        )
        assert cur.fetchone()[0] >= 1
    conn.close()


def test_valid_signature_accepted():
    body = _body()
    sig = sign(body)
    resp = client.post(
        "/github/webhook",
        content=body,
        headers={"X-GitHub-Event": "issues", "X-GitHub-Delivery": "d6", "X-Hub-Signature-256": sig},
    )
    assert resp.status_code == 200
    assert resp.json()["path"] == "new_task"
