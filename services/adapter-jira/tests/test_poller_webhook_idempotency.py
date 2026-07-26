"""WSA-E5-T2 — fallback poller + webhook×poller idempotency.

Proof (against a real Postgres): reconciling an issue via the poller and then
receiving the webhook for the SAME issue (or vice versa) does NOT duplicate —
both derive the same `event_id` from the issue state, and the second one
dedupes."""
from __future__ import annotations

import json

import psycopg2
from fastapi.testclient import TestClient

from adapter_jira.app import app
from adapter_jira.backend import FakeJiraClient
from adapter_jira.poller import JiraPoller
from .helpers import sign

client = TestClient(app)
DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
TENANT_ID = "test_tenant_jira_adapter"
SITE = "https://acme.atlassian.net"


def _issue(key, issue_id, *, labels=None, status="To Do"):
    return {
        "id": issue_id,
        "key": key,
        "self": f"{SITE}/rest/api/3/issue/{issue_id}",
        "fields": {
            "summary": "Poller task",
            "description": "details",
            "labels": labels or [],
            "status": {"name": status},
            "project": {"key": key.split("-")[0]},
            "reporter": {"accountId": "acc_reporter", "displayName": "Rita"},
        },
    }


def _webhook_issue_created(issue):
    payload = {
        "webhookEvent": "jira:issue_created",
        "user": {"accountId": "acc_reporter", "displayName": "Rita"},
        "issue": issue,
    }
    body = json.dumps(payload).encode()
    resp = client.post("/jira/webhook", content=body, headers={"X-Hub-Signature": sign(body)})
    assert resp.status_code == 200
    return resp.json()


def _count_task_events(ticket_key):
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM ingest_events ie JOIN work_items wi ON wi.id=ie.work_item_id "
            "WHERE wi.source_ref @> %s::jsonb AND ie.kind='task_request'",
            (json.dumps({"ticket_key": ticket_key}),),
        )
        return cur.fetchone()[0]


def _poller(client_fake):
    return JiraPoller(
        client_fake,
        tenant_id=TENANT_ID,
        projects=["DSE"],
        trigger_label="dse",
        approved_status="Plano aprovado",
        rejected_status="Plano rejeitado",
        grace_seconds=0,
        reconcile_comments=False,
    )


def test_poller_creates_task_then_webhook_does_not_duplicate():
    issue = _issue("DSE-700", "10700", labels=["dse"])
    fake = FakeJiraClient(issues_by_project={"DSE": [issue]})

    reconciled = _poller(fake).poll_once()
    assert reconciled >= 1
    assert _count_task_events("DSE-700") == 1

    # the webhook arrives afterwards for the SAME issue -> dedup, nothing new.
    _webhook_issue_created(issue)
    assert _count_task_events("DSE-700") == 1


def test_webhook_creates_task_then_poller_does_not_duplicate():
    issue = _issue("DSE-701", "10701", labels=["dse"])
    _webhook_issue_created(issue)
    assert _count_task_events("DSE-701") == 1

    fake = FakeJiraClient(issues_by_project={"DSE": [issue]})
    _poller(fake).poll_once()
    assert _count_task_events("DSE-701") == 1  # the poller reconciled the same fact, without duplicating


def test_poller_advances_cursor():
    issue = _issue("DSE-702", "10702", labels=["dse"])
    fake = FakeJiraClient(issues_by_project={"DSE": [issue]})
    _poller(fake).poll_once()

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT last_polled_at FROM jira_poll_state WHERE tenant_id=%s AND project_key='DSE'", (TENANT_ID,)
        )
        row = cur.fetchone()
    conn.close()
    assert row is not None and row[0] is not None
