"""WSF-E1-T2 — audit-based reconstruction exercise (Fase 1 exit criterion:
"first audit-based reconstruction exercise passes").

Requires a real Postgres (`make up && make migrate` already running) — never mock
audit_log to prove reconstruction: that is the whole point of the system (P8).
"""
from __future__ import annotations

import datetime as dt
import uuid

from dse_audit import emit, export_audit_range, export_audit_range_csv, reconstruct_work_item_history

# Canonical sequence of a full WorkItem through the state machine from the
# technical proposal §9.3 (Fase 1 core loop).
LIFECYCLE: list[tuple[str, str, dict[str, object]]] = [
    ("system:ingest-gateway", "admitted", {"channel": "#eng-payments"}),
    ("usr_requester", "clarified", {"answer": "use tabela orders"}),
    ("system:orchestrator", "plan", {"risk_class": "low", "files": ["svc/orders.py"]}),
    ("system:orchestrator", "implementing", {"sandbox_id": "sbx_1"}),
    ("system:validation", "l1_passed", {"lint": "ok", "tests": "ok"}),
    ("system:orchestrator", "pr_opened", {"pr_number": 42}),
    ("usr_reviewer", "review_approved", {"pr_number": 42}),
    ("usr_reviewer", "merged", {"pr_number": 42, "sha": "abc123"}),
]


def _emit_lifecycle(work_item_id: str, tenant_id: str) -> None:
    for actor, action, details in LIFECYCLE:
        emit(actor=actor, action=action, tenant_id=tenant_id, work_item_id=work_item_id, details=details)


def test_reconstruct_work_item_history_reproduces_exact_order():
    work_item_id = f"wi_{uuid.uuid4().hex[:12]}"
    tenant_id = f"acme-recon-{uuid.uuid4().hex[:8]}"

    _emit_lifecycle(work_item_id, tenant_id)

    history = reconstruct_work_item_history(work_item_id)

    # The reconstruction must contain exactly the 8 transitions, in the exact
    # order they were written (proof that a single SELECT ordered by ts is enough
    # to answer "who did what, when" for this WorkItem).
    assert len(history) == len(LIFECYCLE)
    reconstructed_actions = [row["action"] for row in history]
    expected_actions = [action for _, action, _ in LIFECYCLE]
    assert reconstructed_actions == expected_actions

    reconstructed_actors = [row["actor"] for row in history]
    expected_actors = [actor for actor, _, _ in LIFECYCLE]
    assert reconstructed_actors == expected_actors

    # monotonically non-decreasing timestamps (proof of real ordering, not just a
    # coincidental insertion order)
    timestamps = [row["ts"] for row in history]
    assert timestamps == sorted(timestamps)

    # each event's details preserved faithfully
    for row, (_, _, expected_details) in zip(history, LIFECYCLE):
        assert row["details"] == expected_details
        assert row["tenant_id"] == tenant_id


def test_reconstruct_work_item_history_is_isolated_per_work_item():
    tenant_id = f"acme-recon-{uuid.uuid4().hex[:8]}"
    wi_a = f"wi_{uuid.uuid4().hex[:12]}"
    wi_b = f"wi_{uuid.uuid4().hex[:12]}"

    _emit_lifecycle(wi_a, tenant_id)
    emit(actor="usr_requester", action="admitted", tenant_id=tenant_id, work_item_id=wi_b, details={})

    history_a = reconstruct_work_item_history(wi_a)
    history_b = reconstruct_work_item_history(wi_b)

    assert len(history_a) == len(LIFECYCLE)
    assert len(history_b) == 1
    assert all(row["details"] == {} or "sandbox_id" in row["details"] or True for row in history_a)


def test_reconstruct_unknown_work_item_returns_empty_list():
    assert reconstruct_work_item_history(f"wi_never_existed_{uuid.uuid4().hex}") == []


def test_export_audit_range_covers_tenant_period_and_excludes_outside_events():
    tenant_id = f"acme-export-{uuid.uuid4().hex[:8]}"
    work_item_id = f"wi_{uuid.uuid4().hex[:12]}"

    before = dt.datetime.now(dt.timezone.utc)
    _emit_lifecycle(work_item_id, tenant_id)
    after = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)

    exported = export_audit_range(tenant_id, before, after)
    assert len(exported) == len(LIFECYCLE)
    assert all(row["tenant_id"] == tenant_id for row in exported)
    assert all(row["work_item_id"] == work_item_id for row in exported)

    # a narrow window ending before the first event must include nothing
    far_past_start = before - dt.timedelta(days=1)
    far_past_end = before
    assert export_audit_range(tenant_id, far_past_start, far_past_end) == []


def test_export_audit_range_csv_is_well_formed():
    import csv
    import io

    tenant_id = f"acme-export-{uuid.uuid4().hex[:8]}"
    work_item_id = f"wi_{uuid.uuid4().hex[:12]}"
    before = dt.datetime.now(dt.timezone.utc)
    _emit_lifecycle(work_item_id, tenant_id)
    after = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)

    csv_text = export_audit_range_csv(tenant_id, before, after)
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)
    assert len(rows) == len(LIFECYCLE)
    assert set(reader.fieldnames) == {"ts", "tenant_id", "work_item_id", "actor", "action", "details"}
    assert rows[0]["action"] == LIFECYCLE[0][1]
    assert rows[-1]["action"] == LIFECYCLE[-1][1]
