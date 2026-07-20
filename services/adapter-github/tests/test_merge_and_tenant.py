"""WSA-E4-T3 (webhook pull_request merged -> signal merged_by_human) e
WSA-E1-T5 (resolução installation GitHub -> tenant)."""
from __future__ import annotations

import json

import psycopg2
from fastapi.testclient import TestClient

from adapter_github.app import app
from .helpers import sign

client = TestClient(app)
DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
TENANT_ID = "test_tenant_github_adapter"


def _post(payload: dict, event_type: str, delivery_id: str) -> dict:
    body = json.dumps(payload).encode()
    resp = client.post(
        "/github/webhook",
        content=body,
        headers={"X-GitHub-Event": event_type, "X-GitHub-Delivery": delivery_id, "X-Hub-Signature-256": sign(body)},
    )
    assert resp.status_code == 200
    return resp.json()


def _create_active_work_item(number: int, delivery: str) -> str:
    data = _post(
        {
            "action": "labeled",
            "issue": {"number": number, "title": "t", "body": "b"},
            "label": {"name": "dse"},
            "repository": {"full_name": "acme/widgets"},
            "sender": {"login": "alice"},
        },
        "issues",
        delivery,
    )
    assert data["path"] == "new_task"
    return data["work_item_id"]


def test_pull_request_merged_records_merged_by_human_signal():
    work_item_id = _create_active_work_item(810, "d-810")

    data = _post(
        {
            "action": "closed",
            "pull_request": {
                "number": 810,
                "merged": True,
                "merged_by": {"login": "maintainer"},
                "merge_commit_sha": "abc123sha",
            },
            "repository": {"full_name": "acme/widgets"},
            "sender": {"login": "maintainer"},
        },
        "pull_request",
        "d-811",
    )
    assert data["path"] == "signal_merged_by_human"
    assert data["work_item_id"] == work_item_id

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT payload FROM ingest_events WHERE work_item_id=%s AND kind='approval' ORDER BY id DESC LIMIT 1",
            (work_item_id,),
        )
        payload = cur.fetchone()[0]
    conn.close()
    assert payload["merged_by_human"] is True
    assert payload["pr_number"] == 810


def test_pull_request_closed_without_merge_does_not_signal():
    work_item_id = _create_active_work_item(820, "d-820")

    data = _post(
        {
            "action": "closed",
            "pull_request": {"number": 820, "merged": False, "merge_commit_sha": None},
            "repository": {"full_name": "acme/widgets"},
            "sender": {"login": "maintainer"},
        },
        "pull_request",
        "d-821",
    )
    assert data["path"] == "ignored_pr_closed_unmerged"

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM ingest_events WHERE work_item_id=%s AND kind='approval'", (work_item_id,)
        )
        assert cur.fetchone()[0] == 0
    conn.close()


def test_merge_without_active_work_item_is_ignored():
    data = _post(
        {
            "action": "closed",
            "pull_request": {"number": 830, "merged": True, "merged_by": {"login": "m"}, "merge_commit_sha": "z9"},
            "repository": {"full_name": "acme/widgets"},
            "sender": {"login": "m"},
        },
        "pull_request",
        "d-831",
    )
    assert data["path"] == "ignored_no_active_work_item"


def test_merge_redelivery_is_deduped_by_event_id():
    work_item_id = _create_active_work_item(840, "d-840")
    merge_payload = {
        "action": "closed",
        "pull_request": {"number": 840, "merged": True, "merged_by": {"login": "m"}, "merge_commit_sha": "same-sha"},
        "repository": {"full_name": "acme/widgets"},
        "sender": {"login": "m"},
    }
    _post(merge_payload, "pull_request", "d-841")
    _post(merge_payload, "pull_request", "d-842")  # reentrega (mesmo merge_commit_sha)

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM ingest_events WHERE work_item_id=%s AND kind='approval'", (work_item_id,)
        )
        assert cur.fetchone()[0] == 1  # dedup por event_id (merge_commit_sha estável)
    conn.close()


def test_installation_binding_resolves_tenant(monkeypatch):
    """Um webhook com installation mapeada em tenant_platform_bindings admite
    o WorkItem sob o tenant mapeado (não o DSE_TENANT_ID default)."""
    mapped_tenant = "test_tenant_gh_mapped"
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenant_platform_bindings (platform, binding_key, tenant_id) VALUES ('github', %s, %s) "
            "ON CONFLICT (platform, binding_key) DO UPDATE SET tenant_id = EXCLUDED.tenant_id",
            ("55501", mapped_tenant),
        )
    conn.commit()
    conn.close()

    data = _post(
        {
            "action": "labeled",
            "issue": {"number": 850, "title": "t", "body": "b"},
            "label": {"name": "dse"},
            "repository": {"full_name": "acme/widgets"},
            "sender": {"login": "alice"},
            "installation": {"id": 55501},
        },
        "issues",
        "d-850",
    )
    assert data["path"] == "new_task"

    # Limpeza fica a cargo do fixture `_cleanup` (superuser) — mapped_tenant
    # casa com o prefixo `test_tenant_%`, então work_items/ingest_events/
    # tenant_platform_bindings/audit são removidos automaticamente.
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT tenant_id FROM work_items WHERE id=%s", (data["work_item_id"],))
        assert cur.fetchone()[0] == mapped_tenant
    conn.close()
