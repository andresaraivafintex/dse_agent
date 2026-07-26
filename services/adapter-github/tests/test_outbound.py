"""WSA-E4-T2 — outbound: exactly 1 status comment per issue/PR, edited
in-place, under the GitHub App identity. The documented `FakeGithubClient`
replaces the real HTTP transport; `GithubCommentBackend`/
`MutableCommentWriter` are 100% real."""
from __future__ import annotations

import uuid

import psycopg2
from fastapi.testclient import TestClient

import adapter_github.app as app_module
from adapter_github.backend import FakeGithubClient
from adapter_github.app import app

client = TestClient(app)
DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"


def _make_work_item(tenant_id: str) -> str:
    work_item_id = f"wi_gh_out_{uuid.uuid4().hex[:12]}"
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO work_items (id, tenant_id, source, source_ref, repo, requester, idempotency_key) "
            "VALUES (%s,%s,'github','{}'::jsonb,'acme/widgets','usr_test',%s)",
            (work_item_id, tenant_id, f"idem_{work_item_id}"),
        )
    conn.commit()
    conn.close()
    return work_item_id


def test_first_status_update_creates_a_single_comment(tenant_id, monkeypatch):
    fake_client = FakeGithubClient()
    monkeypatch.setattr(app_module, "build_real_github_client", lambda *, deadline: fake_client)

    work_item_id = _make_work_item(tenant_id)
    resp = client.post(
        "/internal/status-comment",
        json={
            "work_item_id": work_item_id,
            "repo": "acme/widgets",
            "issue_number": 42,
            "body": "Task started",
            "actor": "system:orchestrator",
        },
    )
    assert resp.status_code == 200
    assert len(fake_client.create_calls) == 1
    assert len(fake_client.update_calls) == 0


def test_subsequent_updates_edit_in_place_never_create_new_comment(tenant_id, monkeypatch):
    fake_client = FakeGithubClient()
    monkeypatch.setattr(app_module, "build_real_github_client", lambda *, deadline: fake_client)

    work_item_id = _make_work_item(tenant_id)
    for body in ["Task started", "L1 validating", "PR opened"]:
        resp = client.post(
            "/internal/status-comment",
            json={
                "work_item_id": work_item_id,
                "repo": "acme/widgets",
                "issue_number": 42,
                "body": body,
                "actor": "system:orchestrator",
            },
        )
        assert resp.status_code == 200

    assert len(fake_client.create_calls) == 1
    assert len(fake_client.update_calls) == 2
    (comment_id, text), = [(cid, b) for cid, b in fake_client.comments.items()]
    assert text == "PR opened"
