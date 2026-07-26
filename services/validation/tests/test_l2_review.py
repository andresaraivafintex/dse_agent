"""WSE-E2-T4 — L2 review orchestration (fresh context).

Postgres is REAL (we never mock the evidence/idempotency guarantee, P8). The L2
SESSION is a deterministic `FakeL2ReviewSession` because the real session belongs
to WS-C and is being built in parallel (see README) — only the model call is
fake, the orchestration (verdict+cost recording, cheapest-first guard, P3) is the
production one."""
from __future__ import annotations


import pytest
from dse_contracts import L1Finding, L1Result, PlanArtifact

from dse_validation import db
from dse_validation.l2.l2_review import L2PreconditionError, guard_l2_after_l1, run_l2_review
from dse_validation.l2.session import FakeL2ReviewSession, L2ReviewInput


def _plan(work_item_id: str) -> PlanArtifact:
    return PlanArtifact(work_item_id=work_item_id, steps=["mudar X"], expected_files=["app.py"])


def test_l2_input_carries_only_plan_and_diff_p3(work_item_id, tenant_id):
    """Structural P3: `L2ReviewInput` has NO Coder-history field whatsoever;
    the L2 session can only see plan+diff. Proven via the schema + what the
    session actually receives."""
    fields = set(L2ReviewInput.model_fields.keys())
    assert fields == {"work_item_id", "tenant_id", "plan", "diff", "iteration"}
    assert not any("coder" in f or "history" in f or "transcript" in f for f in fields)

    session = FakeL2ReviewSession()
    inp = L2ReviewInput(
        work_item_id=work_item_id, tenant_id=tenant_id, plan=_plan(work_item_id), diff="+ new line"
    )
    run_l2_review(session, work_item_id=work_item_id, tenant_id=tenant_id, inp=inp)
    assert len(session.calls) == 1
    seen = session.calls[0]
    assert seen.diff == "+ new line"
    assert not hasattr(seen, "coder_history")


def test_run_l2_review_records_verdict_and_cost(work_item_id, tenant_id):
    session = FakeL2ReviewSession(scripted=[(True, [])], cost_usd=0.037)
    inp = L2ReviewInput(work_item_id=work_item_id, tenant_id=tenant_id, plan=_plan(work_item_id), diff="d")
    verdict = run_l2_review(session, work_item_id=work_item_id, tenant_id=tenant_id, inp=inp, iteration=0)

    assert verdict.passed is True
    assert verdict.cost_usd == pytest.approx(0.037)

    rows = db.get_l2_reviews(work_item_id)
    assert len(rows) == 1
    assert rows[0]["passed"] is True
    assert rows[0]["cost_usd"] == pytest.approx(0.037)
    assert rows[0]["iteration"] == 0


def test_run_l2_review_records_objections_when_failed(work_item_id, tenant_id):
    session = FakeL2ReviewSession(scripted=[(False, ["app.py:12 divides without a zero check"])])
    inp = L2ReviewInput(work_item_id=work_item_id, tenant_id=tenant_id, plan=_plan(work_item_id), diff="d")
    verdict = run_l2_review(session, work_item_id=work_item_id, tenant_id=tenant_id, inp=inp, iteration=1)

    assert verdict.passed is False
    assert "app.py:12" in verdict.objections[0]
    rows = db.get_l2_reviews(work_item_id)
    assert rows[0]["objections"] == ["app.py:12 divides without a zero check"]
    assert rows[0]["iteration"] == 1


def test_multiple_iterations_recorded_in_order(work_item_id, tenant_id):
    session = FakeL2ReviewSession(scripted=[(False, ["o1"]), (False, ["o2"]), (True, [])])
    plan = _plan(work_item_id)
    for i in range(3):
        inp = L2ReviewInput(work_item_id=work_item_id, tenant_id=tenant_id, plan=plan, diff=f"d{i}")
        run_l2_review(session, work_item_id=work_item_id, tenant_id=tenant_id, inp=inp, iteration=i)

    rows = db.get_l2_reviews(work_item_id)
    assert [r["iteration"] for r in rows] == [0, 1, 2]
    assert [r["passed"] for r in rows] == [False, False, True]


def test_guard_l2_after_l1_blocks_when_l1_red(work_item_id):
    red = L1Result(
        work_item_id=work_item_id,
        passed=False,
        findings=[L1Finding(check="test", passed=False, detail="1 failing")],
    )
    with pytest.raises(L2PreconditionError):
        guard_l2_after_l1(red)


def test_guard_l2_after_l1_allows_when_l1_green(work_item_id):
    green = L1Result(
        work_item_id=work_item_id,
        passed=True,
        findings=[L1Finding(check="test", passed=True)],
    )
    guard_l2_after_l1(green)  # does not raise
