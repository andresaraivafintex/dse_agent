"""WSA-E6-T3 — signal routing by WorkItem STATUS + merge override
(WSA-E4-T3).

Two layers:
  1. `_route_signal(status, kind, payload)` — pure decision logic
     (deterministic, P1): tested exhaustively branch by branch.
  2. Real integration (Postgres + Temporal): the `Dispatcher` drains an
     approval ingest_event and emits the audit row with the correct signal,
     consuming the event (never mocked — CONVENTIONS.md).
"""
from __future__ import annotations

import asyncio
import json
import uuid

import psycopg2
import pytest
from temporalio.client import Client

from dse_contracts import TASK_QUEUE, WORKFLOW_TYPE
from dse_contracts.constants import (
    SIGNAL_CLARIFICATION_ANSWER,
    SIGNAL_MERGED_BY_HUMAN,
    SIGNAL_PLAN_APPROVAL,
    SIGNAL_REVIEW_COMMENT,
)
from ingest_gateway.dispatcher import DispatchOutcome, _dispatch_row, _route_signal

DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
TEMPORAL_ADDRESS = "localhost:7233"


# --------------------------------------------------------------------------
# 1. Pure routing logic
# --------------------------------------------------------------------------
def _payload(content="ok", **extra):
    p = {"content_snapshot": content, "actor": {"resolved_principal": "usr_alice"}}
    p.update(extra)
    return p


def test_approval_with_awaiting_plan_approval_routes_to_plan_approval():
    route = _route_signal("awaiting_plan_approval", "approval", _payload())
    assert route.signal_name == SIGNAL_PLAN_APPROVAL
    assert route.payload["verdict"] == "approved"
    assert route.payload["actor"] == "usr_alice"


def test_approval_rejected_carries_route_when_rejected():
    route = _route_signal(
        "awaiting_plan_approval", "approval", _payload(approval_verdict="rejected", approval_route="re_clarify")
    )
    assert route.signal_name == SIGNAL_PLAN_APPROVAL
    assert route.payload["verdict"] == "rejected"
    assert route.payload["route"] == "re_clarify"


def test_approval_rejected_defaults_route_when_missing():
    route = _route_signal("awaiting_plan_approval", "approval", _payload(approval_verdict="rejected"))
    assert route.payload["verdict"] == "rejected"
    assert route.payload["route"] == "re_plan"  # required when rejected (WSB-E3-T3)


def test_approval_with_pr_ready_routes_to_review_comment():
    route = _route_signal("pr_ready", "approval", _payload())
    assert route.signal_name == SIGNAL_REVIEW_COMMENT
    assert route.payload["verdict"] == "approved"


def test_approval_with_unexpected_status_declines_never_guesses():
    route = _route_signal("implementing", "approval", _payload())
    assert route.signal_name is None
    assert route.reason == "unexpected_status"


def test_merge_marker_routes_to_merged_by_human_regardless_of_status():
    route = _route_signal(
        "pr_ready", "approval", _payload(merged_by_human=True, merged_by="usr_bob", pr_number=42)
    )
    assert route.signal_name == SIGNAL_MERGED_BY_HUMAN
    assert route.payload["merged_by"] == "usr_bob"
    assert route.payload["pr_number"] == 42


def test_clarification_answer_preserved_from_phase1():
    route = _route_signal("needs_clarification", "clarification_answer", _payload("resposta"))
    assert route.signal_name == SIGNAL_CLARIFICATION_ANSWER
    assert route.payload["acceptance_criteria"] == "resposta"


def test_review_comment_with_verdict_preserved():
    p = _payload("muda isso", source_ref={"review_state": "changes_requested"})
    route = _route_signal("pr_ready", "review_comment", p)
    assert route.signal_name == SIGNAL_REVIEW_COMMENT
    assert route.payload["verdict"] == "changes_requested"


def test_review_comment_without_verdict_is_not_a_decision():
    route = _route_signal("pr_ready", "review_comment", _payload("just a comment"))
    assert route.signal_name is None
    assert route.reason == "review_comment_no_verdict"


# --------------------------------------------------------------------------
# 2. Real integration (Postgres + Temporal)
#
# Isolation note: this phase's shared environment has the
# `dse_ingest_dispatcher` container (Phase 1 code, old `kind`-based routing)
# running `run_forever` against the SAME outbox. A test that inserted into
# `ingest_events` and called `drain_once` would compete with that container
# (SKIP LOCKED) — and, worse, the container routes with the old map. That is
# why the routing INTEGRATION here exercises `_dispatch_row` DIRECTLY (the new
# code, WSA-E6-T3), delivering a REAL signal to a REAL Temporal workflow —
# genuine signal durability/delivery (P8), without going through the contended
# outbox.
# --------------------------------------------------------------------------
def _create_work_item_and_workflow(client, tenant_id: str, status: str, *, start: bool = True) -> str:
    work_item_id = f"wi_route_{uuid.uuid4().hex[:12]}"
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO work_items (id, tenant_id, source, source_ref, requester, idempotency_key, status) "
            "VALUES (%s,%s,'github',%s::jsonb,'usr_alice',%s,%s)",
            (work_item_id, tenant_id, json.dumps({"repo": "acme/x", "number": 1}), f"idem_{work_item_id}", status),
        )
    conn.commit()
    conn.close()
    if start:
        asyncio.get_event_loop().run_until_complete(
            client.start_workflow(WORKFLOW_TYPE, work_item_id, id=work_item_id, task_queue=TASK_QUEUE)
        )
    return work_item_id


@pytest.fixture
def loop():
    return asyncio.new_event_loop()


@pytest.fixture
def temporal_client(loop):
    return loop.run_until_complete(Client.connect(TEMPORAL_ADDRESS))


def test_approval_dispatch_signals_plan_approval_when_awaiting(tenant_id, temporal_client, loop):
    wi = _create_work_item_and_workflow(temporal_client, tenant_id, "awaiting_plan_approval")
    outcome, details = loop.run_until_complete(
        _dispatch_row(
            temporal_client,
            work_item_id=wi,
            event_id="evt_x",
            kind="approval",
            status="awaiting_plan_approval",
            payload={"content_snapshot": "aprovado", "actor": {"resolved_principal": "usr_alice"}},
        )
    )
    assert outcome == DispatchOutcome.SIGNALED
    assert details["signal"] == SIGNAL_PLAN_APPROVAL


def test_merge_event_dispatch_signals_merged_by_human(tenant_id, temporal_client, loop):
    wi = _create_work_item_and_workflow(temporal_client, tenant_id, "pr_ready")
    outcome, details = loop.run_until_complete(
        _dispatch_row(
            temporal_client,
            work_item_id=wi,
            event_id="evt_y",
            kind="approval",
            status="pr_ready",
            payload={"content_snapshot": "merged", "merged_by_human": True, "merged_by": "usr_bob", "pr_number": 2},
        )
    )
    assert outcome == DispatchOutcome.SIGNALED
    assert details["signal"] == SIGNAL_MERGED_BY_HUMAN


def test_approval_unexpected_status_is_declined_never_guesses(tenant_id, temporal_client, loop):
    wi = _create_work_item_and_workflow(temporal_client, tenant_id, "implementing", start=False)
    outcome, details = loop.run_until_complete(
        _dispatch_row(
            temporal_client,
            work_item_id=wi,
            event_id="evt_z",
            kind="approval",
            status="implementing",
            payload={"content_snapshot": "aprovado?", "actor": {"resolved_principal": "usr_alice"}},
        )
    )
    assert outcome == DispatchOutcome.DECLINED_UNEXPECTED_STATUS
    assert details["status"] == "implementing"
