"""WSA-E6-T1 — correlate(): Path A (new_task) vs Path B (signal), regra de
WorkItem terminal (provenance), e gate de steering allowlist (WSA-E6-T2a)."""
from __future__ import annotations

import json
import uuid

import psycopg2
import pytest
from dse_contracts import Actor, ConversationEvent, EventKind, Platform

from ingest_gateway.correlate import correlate
from ingest_gateway.db import get_connection

DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"


def _insert_work_item(tenant_id: str, source_ref: dict, status: str = "implementing") -> str:
    work_item_id = f"wi_test_{uuid.uuid4().hex[:12]}"
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO work_items (id, tenant_id, source, source_ref, requester, status, idempotency_key)
            VALUES (%s, %s, 'slack', %s::jsonb, 'usr_original_requester', %s, %s)
            """,
            (work_item_id, tenant_id, json.dumps(source_ref), status, f"idem_{work_item_id}"),
        )
    conn.commit()
    conn.close()
    return work_item_id


def _event(kind: EventKind, source_ref: dict, principal: str = "usr_someone") -> ConversationEvent:
    return ConversationEvent.build(
        platform=Platform.slack,
        thread_key=json.dumps(source_ref, sort_keys=True),
        message_id=uuid.uuid4().hex,
        kind=kind,
        source_ref=source_ref,
        actor=Actor(platform_user_id="U999", resolved_principal=principal),
        content_snapshot="some content",
        signature_verified=True,
    )


def test_no_match_returns_new_task(tenant_id):
    conn = get_connection()
    ref = {"channel": "C1", "thread_ts": "999.999"}
    event = _event(EventKind.task_request, ref)

    result = correlate(conn, tenant_id=tenant_id, event=event, requester_principal="usr_someone")

    assert result.kind == "new_task"
    assert result.work_item_id is None


def test_match_on_active_work_item_returns_signal(tenant_id):
    ref = {"channel": "C1", "thread_ts": "111.111"}
    wi_id = _insert_work_item(tenant_id, ref, status="implementing")

    conn = get_connection()
    event = _event(EventKind.clarification_answer, ref)
    result = correlate(conn, tenant_id=tenant_id, event=event, requester_principal="usr_someone")

    assert result.kind == "signal"
    assert result.work_item_id == wi_id


def test_match_on_terminal_work_item_returns_new_task_with_provenance(tenant_id):
    ref = {"channel": "C1", "thread_ts": "222.222"}
    old_wi_id = _insert_work_item(tenant_id, ref, status="done")

    conn = get_connection()
    event = _event(EventKind.task_request, ref)
    result = correlate(conn, tenant_id=tenant_id, event=event, requester_principal="usr_someone")

    assert result.kind == "new_task"
    assert result.work_item_id is None
    assert result.provenance_work_item_id == old_wi_id


def test_steering_by_unauthorized_principal_is_rejected(tenant_id):
    ref = {"repo": "acme/widgets", "number": 42}
    wi_id = _insert_work_item(tenant_id, ref, status="implementing")

    conn = get_connection()
    event = _event(EventKind.steering, ref, principal="usr_not_on_allowlist")
    result = correlate(conn, tenant_id=tenant_id, event=event, requester_principal="usr_not_on_allowlist")
    conn.commit()

    assert result.kind == "unauthorized"
    assert result.work_item_id == wi_id

    check_conn = psycopg2.connect(DSN)
    with check_conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM audit_log WHERE tenant_id=%s AND action='steering_rejected_unauthorized' AND work_item_id=%s",
            (tenant_id, wi_id),
        )
        assert cur.fetchone() is not None
    check_conn.close()


def test_steering_by_authorized_principal_is_signal(tenant_id):
    ref = {"repo": "acme/widgets", "number": 43}
    wi_id = _insert_work_item(tenant_id, ref, status="implementing")

    allow_conn = psycopg2.connect(DSN)
    with allow_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenant_steering_allowlist (tenant_id, principal_id) VALUES (%s, %s)",
            (tenant_id, "usr_maintainer"),
        )
    allow_conn.commit()
    allow_conn.close()

    conn = get_connection()
    event = _event(EventKind.steering, ref, principal="usr_maintainer")
    result = correlate(conn, tenant_id=tenant_id, event=event, requester_principal="usr_maintainer")

    assert result.kind == "signal"
    assert result.work_item_id == wi_id


def test_review_comment_is_steering_gated_too(tenant_id):
    ref = {"repo": "acme/widgets", "number": 44}
    _insert_work_item(tenant_id, ref, status="pr_ready")

    conn = get_connection()
    event = _event(EventKind.review_comment, ref, principal="usr_random")
    result = correlate(conn, tenant_id=tenant_id, event=event, requester_principal="usr_random")

    assert result.kind == "unauthorized"


def test_clarification_answer_bypasses_steering_gate(tenant_id):
    """clarification_answer é resposta esperada do próprio fluxo — não passa
    pelo allowlist gate mesmo vindo de um principal não listado."""
    ref = {"channel": "C1", "thread_ts": "333.333"}
    wi_id = _insert_work_item(tenant_id, ref, status="needs_clarification")

    conn = get_connection()
    event = _event(EventKind.clarification_answer, ref, principal="usr_not_on_allowlist")
    result = correlate(conn, tenant_id=tenant_id, event=event, requester_principal="usr_not_on_allowlist")

    assert result.kind == "signal"
    assert result.work_item_id == wi_id
