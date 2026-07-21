"""WSA-E4-T1 — issue labeled/assigned cria task_request; comentário com
menção numa issue comum cria task_request; comentário em PR (issue_comment
numa PR ou pull_request_review_comment) NUNCA cria WorkItem novo — só
correlaciona (signal) por número de PR/issue."""
from __future__ import annotations

import json

import psycopg2
from dse_identity import resolve_principal
from fastapi.testclient import TestClient

from adapter_github.app import app
from .helpers import sign

TENANT_ID = "test_tenant_github_adapter"


def _allow_steering(login: str) -> str:
    """Resolve o principal de `login` e o adiciona à allowlist de steering do
    tenant de teste (WSA-E6-T2a) — sem isso, review_comment/steering de um
    principal não listado é rejeitado por design."""
    principal_id = resolve_principal("github", login, login)
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenant_steering_allowlist (tenant_id, principal_id) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING",
            (TENANT_ID, principal_id),
        )
    conn.commit()
    conn.close()
    return principal_id

client = TestClient(app)
DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"


def _post(payload: dict, event_type: str, delivery_id: str) -> dict:
    body = json.dumps(payload).encode()
    sig = sign(body)
    resp = client.post(
        "/github/webhook",
        content=body,
        headers={"X-GitHub-Event": event_type, "X-GitHub-Delivery": delivery_id, "X-Hub-Signature-256": sig},
    )
    assert resp.status_code == 200
    return resp.json()


def test_issue_labeled_dse_creates_new_task():
    data = _post(
        {
            "action": "labeled",
            "issue": {"number": 100, "title": "Add rate limiting", "body": "please add it"},
            "label": {"name": "dse"},
            "repository": {"full_name": "acme/widgets"},
            "sender": {"login": "alice"},
        },
        "issues",
        "delivery-100",
    )
    assert data["path"] == "new_task"

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT status, source, repo FROM work_items WHERE id = %s", (data["work_item_id"],))
        assert cur.fetchone() == ("new", "github", "acme/widgets")
    conn.close()


def test_issue_labeled_other_label_is_ignored():
    data = _post(
        {
            "action": "labeled",
            "issue": {"number": 101, "title": "t", "body": "b"},
            "label": {"name": "bug"},
            "repository": {"full_name": "acme/widgets"},
            "sender": {"login": "alice"},
        },
        "issues",
        "delivery-101",
    )
    assert data["path"] == "ignored_label"


def test_issue_assigned_creates_new_task():
    data = _post(
        {
            "action": "assigned",
            "issue": {"number": 102, "title": "t", "body": "b"},
            "repository": {"full_name": "acme/widgets"},
            "sender": {"login": "alice"},
        },
        "issues",
        "delivery-102",
    )
    assert data["path"] == "new_task"


def test_issue_comment_with_mention_creates_new_task():
    data = _post(
        {
            "action": "created",
            "issue": {"number": 200, "title": "t", "body": "b"},
            "comment": {"id": 5001, "body": "@dse-bot please handle this", "user": {"login": "bob"}},
            "repository": {"full_name": "acme/widgets"},
        },
        "issue_comment",
        "delivery-200",
    )
    assert data["path"] == "new_task"


def test_issue_comment_without_mention_and_no_active_work_item_is_ignored():
    data = _post(
        {
            "action": "created",
            "issue": {"number": 201, "title": "t", "body": "b"},
            "comment": {"id": 5002, "body": "just a random comment", "user": {"login": "bob"}},
            "repository": {"full_name": "acme/widgets"},
        },
        "issue_comment",
        "delivery-201",
    )
    assert data["path"] == "ignored_no_mention"

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM work_items WHERE tenant_id='test_tenant_github_adapter' "
            "AND source_ref @> %s::jsonb",
            (json.dumps({"repo": "acme/widgets", "number": 201}),),
        )
        assert cur.fetchone()[0] == 0
    conn.close()


def test_pr_issue_comment_never_creates_work_item_even_without_match():
    """comentário em PR SEM WorkItem ativo correspondente -> ZERO WorkItems
    criados (WSA-E4-T1), diferente do comportamento de issue comum."""
    data = _post(
        {
            "action": "created",
            "issue": {"number": 300, "pull_request": {"url": "https://api.github.com/x"}},
            "comment": {"id": 5003, "body": "looks good to me", "user": {"login": "carol"}},
            "repository": {"full_name": "acme/widgets"},
        },
        "issue_comment",
        "delivery-300",
    )
    assert data["path"] == "ignored_no_active_work_item"

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM work_items WHERE tenant_id='test_tenant_github_adapter' "
            "AND source_ref @> %s::jsonb",
            (json.dumps({"repo": "acme/widgets", "number": 300}),),
        )
        assert cur.fetchone()[0] == 0
    conn.close()


def test_pr_comment_on_active_work_item_correlates_as_signal_zero_new_work_items():
    created = _post(
        {
            "action": "labeled",
            "issue": {"number": 400, "title": "t", "body": "b"},
            "label": {"name": "dse"},
            "repository": {"full_name": "acme/widgets"},
            "sender": {"login": "alice"},
        },
        "issues",
        "delivery-400",
    )
    work_item_id = created["work_item_id"]
    _allow_steering("carol")

    review = _post(
        {
            "action": "created",
            "issue": {"number": 400, "pull_request": {"url": "https://api.github.com/x"}},
            "comment": {"id": 5004, "body": "please rename this variable", "user": {"login": "carol"}},
            "repository": {"full_name": "acme/widgets"},
        },
        "issue_comment",
        "delivery-401",
    )
    assert review["path"] == "signal"
    assert review["work_item_id"] == work_item_id

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        # ainda só 1 work_item para o número 400 — o review comment não criou outro.
        cur.execute(
            "SELECT count(*) FROM work_items WHERE tenant_id='test_tenant_github_adapter' "
            "AND source_ref @> %s::jsonb",
            (json.dumps({"repo": "acme/widgets", "number": 400}),),
        )
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT kind FROM ingest_events WHERE work_item_id=%s ORDER BY id", (work_item_id,))
        kinds = [r[0] for r in cur.fetchall()]
        assert kinds == ["task_request", "review_comment"]
    conn.close()


def test_pr_comment_by_unauthorized_principal_is_rejected_not_signaled():
    """WSA-E6-T2a: review_comment de um principal fora da allowlist do
    tenant é rejeitado (não vira signal), mesmo havendo um WorkItem ativo
    correspondente."""
    created = _post(
        {
            "action": "labeled",
            "issue": {"number": 450, "title": "t", "body": "b"},
            "label": {"name": "dse"},
            "repository": {"full_name": "acme/widgets"},
            "sender": {"login": "alice"},
        },
        "issues",
        "delivery-450",
    )
    work_item_id = created["work_item_id"]

    review = _post(
        {
            "action": "created",
            "issue": {"number": 450, "pull_request": {"url": "https://api.github.com/x"}},
            "comment": {"id": 5010, "body": "do something else entirely", "user": {"login": "random_outsider"}},
            "repository": {"full_name": "acme/widgets"},
        },
        "issue_comment",
        "delivery-451",
    )
    assert review["path"] == "unauthorized"

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM audit_log WHERE tenant_id=%s AND action='steering_rejected_unauthorized' "
            "AND work_item_id=%s",
            (TENANT_ID, work_item_id),
        )
        assert cur.fetchone()[0] == 1
    conn.close()


def test_pull_request_review_comment_event_correlates_as_signal():
    created = _post(
        {
            "action": "labeled",
            "issue": {"number": 500, "title": "t", "body": "b"},
            "label": {"name": "dse"},
            "repository": {"full_name": "acme/widgets"},
            "sender": {"login": "alice"},
        },
        "issues",
        "delivery-500",
    )
    work_item_id = created["work_item_id"]
    _allow_steering("dave")

    review = _post(
        {
            "action": "created",
            "pull_request": {"number": 500},
            "comment": {"id": 5005, "body": "nit: naming", "user": {"login": "dave"}},
            "repository": {"full_name": "acme/widgets"},
        },
        "pull_request_review_comment",
        "delivery-501",
    )
    assert review["path"] == "signal"
    assert review["work_item_id"] == work_item_id


def test_toctou_snapshot_not_refetched_on_redelivery_with_edited_body():
    original = _post(
        {
            "action": "labeled",
            "issue": {"number": 600, "title": "Original title", "body": "Original body"},
            "label": {"name": "dse"},
            "repository": {"full_name": "acme/widgets"},
            "sender": {"login": "alice"},
        },
        "issues",
        "delivery-600",
    )
    work_item_id = original["work_item_id"]

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT payload FROM ingest_events WHERE work_item_id = %s", (work_item_id,))
        payload = cur.fetchone()[0]
    conn.close()
    assert "Original title" in payload["content_snapshot"]

    # Redelivery (MESMO X-GitHub-Delivery -> mesmo message_id -> mesmo
    # event_id) com o issue "editado" -> dedup, snapshot não sobrescrito.
    edited = _post(
        {
            "action": "labeled",
            "issue": {"number": 600, "title": "EDITED title", "body": "EDITED body"},
            "label": {"name": "dse"},
            "repository": {"full_name": "acme/widgets"},
            "sender": {"login": "alice"},
        },
        "issues",
        "delivery-600",  # mesmo delivery id = mesma reentrega
    )
    assert edited["work_item_id"] == work_item_id

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT payload FROM ingest_events WHERE work_item_id = %s ORDER BY id", (work_item_id,))
        rows = cur.fetchall()
    conn.close()
    assert len(rows) == 1
    assert "Original title" in rows[0][0]["content_snapshot"]
    assert "EDITED" not in rows[0][0]["content_snapshot"]


def test_secret_pattern_in_comment_is_redacted_in_sanitized_content():
    data = _post(
        {
            "action": "created",
            "issue": {"number": 700, "title": "t", "body": "b"},
            "comment": {
                "id": 5006,
                "body": "@dse-bot use this token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 to deploy",
                "user": {"login": "bob"},
            },
            "repository": {"full_name": "acme/widgets"},
        },
        "issue_comment",
        "delivery-700",
    )
    assert data["path"] == "new_task"
    work_item_id = data["work_item_id"]

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT payload FROM ingest_events WHERE work_item_id = %s", (work_item_id,))
        payload = cur.fetchone()[0]
    conn.close()

    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" in payload["content_snapshot"]
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in payload["sanitized_content"]


def test_pull_request_review_submitted_carries_review_state_for_verdict():
    """Auditoria pós-S7: o webhook `pull_request_review` (Request changes /
    Approve na UI) é o ÚNICO com `review.state`; sem tratá-lo, o dispatcher
    nunca via um verdict e o loop de changes_requested era inalcançável.
    Valida: correlaciona como signal + `review_state` no source_ref do evento
    (é dele que `_review_signal_payload` deriva o verdict)."""
    created = _post(
        {
            "action": "labeled",
            "issue": {"number": 700, "title": "t", "body": "b"},
            "label": {"name": "dse"},
            "repository": {"full_name": "acme/widgets"},
            "sender": {"login": "alice"},
        },
        "issues",
        "delivery-700",
    )
    work_item_id = created["work_item_id"]
    _allow_steering("erin")

    review = _post(
        {
            "action": "submitted",
            "pull_request": {"number": 700},
            "review": {
                "id": 7007,
                "state": "changes_requested",
                "body": "Remova o arquivo .dse-task-branch do PR",
                "user": {"login": "erin"},
            },
            "repository": {"full_name": "acme/widgets"},
        },
        "pull_request_review",
        "delivery-701",
    )
    assert review["path"] == "signal"
    assert review["work_item_id"] == work_item_id

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT payload FROM ingest_events WHERE work_item_id = %s AND event_id IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (work_item_id,),
        )
        payload = cur.fetchone()[0]
    conn.close()
    assert payload["source_ref"]["review_state"] == "changes_requested"
    assert ".dse-task-branch" in payload["content_snapshot"]
