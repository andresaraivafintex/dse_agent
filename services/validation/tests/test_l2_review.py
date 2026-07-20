"""WSE-E2-T4 — orquestração da revisão L2 (contexto fresco).

Postgres é REAL (nunca mockamos a garantia de evidência/idempotência, P8). A
SESSÃO L2 é um `FakeL2ReviewSession` determinístico porque a sessão real é do
WS-C, construída em paralelo (ver README) — só a chamada de modelo é fake, a
orquestração (recording de veredito+custo, guard cheapest-first, P3) é a de
produção."""
from __future__ import annotations

import uuid

import pytest
from dse_contracts import L1Finding, L1Result, L2Verdict, PlanArtifact

from dse_validation import db
from dse_validation.l2.l2_review import L2PreconditionError, guard_l2_after_l1, run_l2_review
from dse_validation.l2.session import FakeL2ReviewSession, L2ReviewInput


def _plan(work_item_id: str) -> PlanArtifact:
    return PlanArtifact(work_item_id=work_item_id, steps=["mudar X"], expected_files=["app.py"])


def test_l2_input_carries_only_plan_and_diff_p3(work_item_id, tenant_id):
    """P3 estrutural: `L2ReviewInput` não tem NENHUM campo de histórico do Coder;
    a sessão L2 só pode ver plan+diff. Prova pelo schema + pelo que a sessão recebe."""
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
    session = FakeL2ReviewSession(scripted=[(False, ["app.py:12 divide sem checar zero"])])
    inp = L2ReviewInput(work_item_id=work_item_id, tenant_id=tenant_id, plan=_plan(work_item_id), diff="d")
    verdict = run_l2_review(session, work_item_id=work_item_id, tenant_id=tenant_id, inp=inp, iteration=1)

    assert verdict.passed is False
    assert "app.py:12" in verdict.objections[0]
    rows = db.get_l2_reviews(work_item_id)
    assert rows[0]["objections"] == ["app.py:12 divide sem checar zero"]
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
    guard_l2_after_l1(green)  # não levanta
