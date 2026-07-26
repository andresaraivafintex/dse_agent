"""WSA-E5-T1 — Jira adapter inbound: label -> task_request, status transition
-> approval (UC5), comment -> clarification; the 4 defenses (signature,
TOCTOU/dedup, sanitization) and per-site tenant resolution."""
from __future__ import annotations

import json

import psycopg2
from fastapi.testclient import TestClient

from adapter_jira.app import app
from .helpers import sign

client = TestClient(app)
DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
TENANT_ID = "test_tenant_jira_adapter"
SITE = "https://acme.atlassian.net"


def _issue(key: str, issue_id: str, *, labels=None, status="To Do", summary="Do the thing",
           desc="details", issue_type="Task"):
    return {
        "id": issue_id,
        "key": key,
        "self": f"{SITE}/rest/api/3/issue/{issue_id}",
        "fields": {
            "summary": summary,
            "description": desc,
            "labels": labels or [],
            "status": {"name": status},
            "issuetype": {"name": issue_type},
            "project": {"key": key.split("-")[0]},
            "reporter": {"accountId": "acc_reporter", "displayName": "Rita Reporter"},
        },
    }


def _post(payload: dict) -> dict:
    body = json.dumps(payload).encode()
    resp = client.post("/jira/webhook", content=body, headers={"X-Hub-Signature": sign(body)})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_bad_signature_is_rejected():
    body = json.dumps({"webhookEvent": "jira:issue_created", "issue": _issue("DSE-1", "10001")}).encode()
    resp = client.post("/jira/webhook", content=body, headers={"X-Hub-Signature": "sha256=deadbeef"})
    assert resp.status_code == 401


def test_issue_created_with_trigger_label_creates_task():
    data = _post(
        {
            "webhookEvent": "jira:issue_created",
            "user": {"accountId": "acc_alice", "displayName": "Alice"},
            "issue": _issue("DSE-10", "10010", labels=["dse"]),
        }
    )
    assert data["path"] == "processed"
    result = data["results"][0]
    assert result["path"] == "new_task"

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT source, source_ref FROM work_items WHERE id=%s", (result["work_item_id"],))
        source, source_ref = cur.fetchone()
    conn.close()
    assert source == "jira"
    assert source_ref == {"ticket_key": "DSE-10"}


def test_issue_type_bug_classifies_task_class(unique_suffix=None):
    """Plan 08 §A (closed in the audit): Jira issue type Bug → task_class
    bug_fix at admission (deterministic _JIRA_TYPE_MAP map)."""
    data = _post(
        {
            "webhookEvent": "jira:issue_created",
            "user": {"accountId": "acc_alice", "displayName": "Alice"},
            "issue": _issue("DSE-77", "10077", labels=["dse"], issue_type="Bug"),
        }
    )
    result = data["results"][0]
    assert result["path"] == "new_task"
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT task_class FROM work_items WHERE id=%s", (result["work_item_id"],))
        assert cur.fetchone()[0] == "bug_fix"
    conn.close()


def test_issue_created_without_label_is_ignored():
    data = _post(
        {
            "webhookEvent": "jira:issue_created",
            "user": {"accountId": "acc_alice", "displayName": "Alice"},
            "issue": _issue("DSE-11", "10011", labels=["bug"]),
        }
    )
    assert data["path"] == "ignored_no_trigger"


def test_status_transition_to_approved_column_signals_approval():
    # create the task first
    created = _post(
        {
            "webhookEvent": "jira:issue_created",
            "user": {"accountId": "acc_alice", "displayName": "Alice"},
            "issue": _issue("DSE-12", "10012", labels=["dse"]),
        }
    )
    work_item_id = created["results"][0]["work_item_id"]

    data = _post(
        {
            "webhookEvent": "jira:issue_updated",
            "user": {"accountId": "acc_boss", "displayName": "Bob Boss"},
            "issue": _issue("DSE-12", "10012", labels=["dse"], status="Plan approved"),
            "changelog": {"id": "cl1", "items": [{"field": "status", "toString": "Plan approved"}]},
        }
    )
    approval = [r for r in data["results"] if r["path"] == "signal_approval"][0]
    assert approval["verdict"] == "approved"
    assert approval["work_item_id"] == work_item_id

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT payload FROM ingest_events WHERE work_item_id=%s AND kind='approval' ORDER BY id DESC LIMIT 1",
            (work_item_id,),
        )
        payload = cur.fetchone()[0]
        # the resolved actor is whoever transitioned it (Bob), not the reporter.
        cur.execute("SELECT display_name FROM principals WHERE id=%s", (payload["actor"]["resolved_principal"],))
        assert cur.fetchone()[0] == "Bob Boss"
    conn.close()
    assert payload["approval_verdict"] == "approved"


def test_status_transition_to_rejected_column_signals_rejection_with_route():
    created = _post(
        {
            "webhookEvent": "jira:issue_created",
            "user": {"accountId": "acc_alice", "displayName": "Alice"},
            "issue": _issue("DSE-13", "10013", labels=["dse"]),
        }
    )
    work_item_id = created["results"][0]["work_item_id"]

    data = _post(
        {
            "webhookEvent": "jira:issue_updated",
            "user": {"accountId": "acc_boss", "displayName": "Bob Boss"},
            "issue": _issue("DSE-13", "10013", labels=["dse"], status="Plan rejected"),
            "changelog": {"id": "cl2", "items": [{"field": "status", "toString": "Plan rejected"}]},
        }
    )
    approval = [r for r in data["results"] if r["path"] == "signal_approval"][0]
    assert approval["verdict"] == "rejected"

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT payload FROM ingest_events WHERE work_item_id=%s AND kind='approval' ORDER BY id DESC LIMIT 1",
            (work_item_id,),
        )
        payload = cur.fetchone()[0]
    conn.close()
    assert payload["approval_verdict"] == "rejected"
    assert payload["approval_route"] == "re_plan"


def test_comment_on_active_ticket_signals_clarification():
    created = _post(
        {
            "webhookEvent": "jira:issue_created",
            "user": {"accountId": "acc_alice", "displayName": "Alice"},
            "issue": _issue("DSE-14", "10014", labels=["dse"]),
        }
    )
    work_item_id = created["results"][0]["work_item_id"]

    data = _post(
        {
            "webhookEvent": "comment_created",
            "issue": _issue("DSE-14", "10014", labels=["dse"]),
            "comment": {"id": "7001", "body": "here is the acceptance criteria", "author": {"accountId": "acc_alice", "displayName": "Alice"}},
        }
    )
    assert data["path"] == "signal"
    assert data["work_item_id"] == work_item_id

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT kind FROM ingest_events WHERE work_item_id=%s ORDER BY id", (work_item_id,))
        kinds = [r[0] for r in cur.fetchall()]
    conn.close()
    assert kinds == ["task_request", "clarification_answer"]


def test_comment_on_non_dse_ticket_is_ignored():
    data = _post(
        {
            "webhookEvent": "comment_created",
            "issue": _issue("DSE-15", "10015"),
            "comment": {"id": "7002", "body": "random", "author": {"accountId": "acc_x", "displayName": "X"}},
        }
    )
    assert data["path"] == "ignored_no_active_work_item"


def test_redelivered_issue_created_is_deduped_toctou_snapshot_frozen():
    payload = {
        "webhookEvent": "jira:issue_created",
        "user": {"accountId": "acc_alice", "displayName": "Alice"},
        "issue": _issue("DSE-16", "10016", labels=["dse"], summary="Original summary"),
    }
    first = _post(payload)
    work_item_id = first["results"][0]["work_item_id"]

    edited = dict(payload)
    edited["issue"] = _issue("DSE-16", "10016", labels=["dse"], summary="EDITED summary")
    _post(edited)  # redelivery: same issue id -> same event_id -> dedup

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT payload FROM ingest_events WHERE work_item_id=%s ORDER BY id", (work_item_id,))
        rows = cur.fetchall()
    conn.close()
    assert len(rows) == 1
    assert "Original summary" in rows[0][0]["content_snapshot"]
    assert "EDITED" not in rows[0][0]["content_snapshot"]


def test_secret_in_description_is_redacted_in_sanitized_content():
    data = _post(
        {
            "webhookEvent": "jira:issue_created",
            "user": {"accountId": "acc_alice", "displayName": "Alice"},
            "issue": _issue(
                "DSE-17", "10017", labels=["dse"], desc="deploy with ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 please"
            ),
        }
    )
    work_item_id = data["results"][0]["work_item_id"]
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT payload FROM ingest_events WHERE work_item_id=%s", (work_item_id,))
        payload = cur.fetchone()[0]
    conn.close()
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" in payload["content_snapshot"]
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in payload["sanitized_content"]


def test_site_binding_resolves_tenant():
    mapped = "test_tenant_jira_mapped"
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenant_platform_bindings (platform, binding_key, tenant_id) VALUES ('jira', %s, %s) "
            "ON CONFLICT (platform, binding_key) DO UPDATE SET tenant_id = EXCLUDED.tenant_id",
            (SITE, mapped),
        )
    conn.commit()
    conn.close()

    data = _post(
        {
            "webhookEvent": "jira:issue_created",
            "user": {"accountId": "acc_alice", "displayName": "Alice"},
            "issue": _issue("DSE-18", "10018", labels=["dse"]),
        }
    )
    work_item_id = data["results"][0]["work_item_id"]
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT tenant_id FROM work_items WHERE id=%s", (work_item_id,))
        assert cur.fetchone()[0] == mapped
    conn.close()
