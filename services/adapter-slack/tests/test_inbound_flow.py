"""WSA-E3-T1 — full inbound flow: app_mention creates a task_request, a reply
in an existing thread correlates via thread_ts (signal), a button interaction
becomes kind=approval, plus TOCTOU snapshot (WSA-E2-T2) and sanitization
(WSA-E2-T3) end-to-end."""
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
        # only 1 work_item created in this flow (the reply does not create a second)
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
    """The recorded snapshot is exactly the text that came in the webhook — we
    prove there is no re-fetch: even simulating an 'edit' via a second webhook
    with different text for the SAME message (same ts), the first ingest_event
    already persisted keeps the original content."""
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

    # Redelivery of the SAME event (same channel+ts) with "edited" text —
    # since platform+thread+message (ts) are identical, the event_id is the
    # SAME and the row is deduplicated (ON CONFLICT DO NOTHING) — the snapshot
    # already recorded is NEVER overwritten by an "edited" version.
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

    assert len(rows) == 1  # dedup — no second row
    assert rows[0][0]["content_snapshot"] == original_text  # was not overwritten


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

    # original snapshot intact (audit) — the secret shows up here on purpose.
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" in payload["content_snapshot"]
    # sanitized version (the one that moves down the pipeline) has the secret redacted.
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


def test_reject_button_carries_rejected_verdict_not_silent_approve():
    """C1 (report 07) — SECURITY regression: a click on 'reject' has to become
    the `approval_verdict=rejected` marker in the signal payload. Without it the
    dispatcher defaults to 'approved' and the rejection would silently approve
    the plan. Mirrors the already-correct Jira path."""
    created = _post_event(
        {
            "type": "app_mention",
            "channel": "C_REJECT",
            "ts": "9100.001",
            "user": "U_REQUESTER",
            "text": "risky migration plan",
        }
    )
    work_item_id = created["work_item_id"]

    data = _post_interaction(
        {
            "type": "block_actions",
            "channel": {"id": "C_REJECT"},
            "message": {"ts": "9100.500", "thread_ts": "9100.001"},
            "user": {"id": "U_REQUESTER"},
            "action_ts": "9100.600",
            "actions": [{"action_id": "dse_plan_reject", "value": "reject:re_plan"}],
        }
    )
    assert data["path"] == "signal"

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT payload FROM ingest_events WHERE work_item_id = %s AND kind='approval' ORDER BY id",
            (work_item_id,),
        )
        payload = cur.fetchone()[0]
    conn.close()

    assert payload.get("approval_verdict") == "rejected"
    assert payload.get("approval_route") == "re_plan"


def test_approve_button_carries_approved_verdict():
    created = _post_event(
        {
            "type": "app_mention",
            "channel": "C_OK",
            "ts": "9200.001",
            "user": "U_REQUESTER",
            "text": "safe change",
        }
    )
    work_item_id = created["work_item_id"]
    _post_interaction(
        {
            "type": "block_actions",
            "channel": {"id": "C_OK"},
            "message": {"ts": "9200.500", "thread_ts": "9200.001"},
            "user": {"id": "U_REQUESTER"},
            "action_ts": "9200.600",
            "actions": [{"action_id": "dse_plan_approve", "value": "approve"}],
        }
    )
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT payload FROM ingest_events WHERE work_item_id = %s AND kind='approval' ORDER BY id",
            (work_item_id,),
        )
        payload = cur.fetchone()[0]
    conn.close()
    assert payload.get("approval_verdict") == "approved"


def test_channel_binding_resolves_repo_at_admission():
    """C2 (report 07) — proof the blocker is resolved: an @mention in a channel
    WITH a repo_binding is born with repo/base_branch filled in (not NULL), so
    the completeness gate passes instead of escalating. This is what was missing
    for a Slack task to run end-to-end."""
    import os
    tenant = os.environ.get("DSE_TENANT_ID", "dev-tenant")
    conn = psycopg2.connect(DSN.replace("dse_app:dse_app_dev_only", "dse:dse_dev_only"))
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO repo_bindings (tenant_id, platform, binding_type, binding_value, repo, base_branch) "
            "VALUES (%s,'slack','channel','C_BOUND','andre2654/fintex-wallet','main') ON CONFLICT DO NOTHING",
            (tenant,),
        )
    conn.commit()
    conn.close()

    created = _post_event(
        {
            "type": "app_mention",
            "channel": "C_BOUND",
            "ts": "9300.001",
            "user": "U_REQUESTER",
            "text": "@fintex-dse fix the balance calculation",
        }
    )
    work_item_id = created["work_item_id"]

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT repo, base_branch FROM work_items WHERE id = %s", (work_item_id,))
        repo, base_branch = cur.fetchone()
    conn.close()
    assert repo == "andre2654/fintex-wallet"
    assert base_branch == "main"


def test_explicit_repo_in_text_overrides():
    """Rung 1 of the cascade: an explicit repo in the text wins (no binding needed)."""
    created = _post_event(
        {
            "type": "app_mention",
            "channel": "C_UNBOUND_XYZ",
            "ts": "9400.001",
            "user": "U_REQUESTER",
            "text": "@fintex-dse repo=andre2654/other-repo do X",
        }
    )
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT repo FROM work_items WHERE id = %s", (created["work_item_id"],))
        repo = cur.fetchone()[0]
    conn.close()
    assert repo == "andre2654/other-repo"
