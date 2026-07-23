"""WSE-E2-T5 — loop de fix-retries bounded L2->Coder.

`decide_next_action` é lógica pura (testada sem I/O). O contador durável
(`register_retry`/`escalate_to_operator`) é testado contra Postgres REAL
(a durabilidade do contador é o próprio ponto — nunca mockado)."""
from __future__ import annotations

import pytest
from dse_contracts import L2Verdict

from dse_validation.config import L2Config
from dse_validation.l2 import fix_loop


def _cfg(max_retries=3, budget=0.0) -> L2Config:
    cfg = L2Config()
    cfg.max_fix_retries = max_retries
    cfg.budget_cap_usd = budget
    return cfg


def _state(work_item_id, tenant_id, iterations=0, spent=0.0) -> fix_loop.FixLoopState:
    return fix_loop.FixLoopState(
        work_item_id=work_item_id, tenant_id=tenant_id, iterations=iterations, spent_usd=spent
    )


# --- decisão pura -----------------------------------------------------------
def test_passed_verdict_proceeds(work_item_id, tenant_id):
    v = L2Verdict(work_item_id=work_item_id, passed=True)
    d = fix_loop.decide_next_action(verdict=v, state=_state(work_item_id, tenant_id), cfg=_cfg())
    assert d.action == "proceed"


def test_failed_verdict_retries_while_under_cap(work_item_id, tenant_id):
    v = L2Verdict(work_item_id=work_item_id, passed=False, objections=["x"])
    d = fix_loop.decide_next_action(
        verdict=v, state=_state(work_item_id, tenant_id, iterations=1), cfg=_cfg(max_retries=3)
    )
    assert d.action == "retry_coder"
    assert d.objections == ["x"]
    assert d.iterations_remaining == 2


def test_escalates_when_retries_exhausted(work_item_id, tenant_id):
    v = L2Verdict(work_item_id=work_item_id, passed=False, objections=["ainda ruim"])
    d = fix_loop.decide_next_action(
        verdict=v, state=_state(work_item_id, tenant_id, iterations=3), cfg=_cfg(max_retries=3)
    )
    assert d.action == "escalate_operator"
    assert "retorno" in d.reason or "cap" in d.reason


def test_escalates_when_budget_exhausted_even_with_retries_left(work_item_id, tenant_id):
    """P6: budget esgotado escala mesmo se ainda há iterações no contador."""
    v = L2Verdict(work_item_id=work_item_id, passed=False, objections=["y"])
    d = fix_loop.decide_next_action(
        verdict=v,
        state=_state(work_item_id, tenant_id, iterations=1, spent=5.0),
        cfg=_cfg(max_retries=5, budget=5.0),
    )
    assert d.action == "escalate_operator"
    assert "budget" in d.reason
    assert d.budget_remaining_usd == 0.0


# --- contador durável (Postgres real) --------------------------------------
def test_register_retry_debits_budget_and_persists(work_item_id, tenant_id):
    state = _state(work_item_id, tenant_id)
    s1 = fix_loop.register_retry(state, coder_cost_usd=0.10, l2_cost_usd=0.02, cfg=_cfg())
    assert s1.iterations == 1
    assert s1.spent_usd == pytest.approx(0.12)

    loaded = fix_loop.load_state(work_item_id, tenant_id)
    assert loaded.iterations == 1
    assert loaded.spent_usd == pytest.approx(0.12)

    s2 = fix_loop.register_retry(loaded, coder_cost_usd=0.05, l2_cost_usd=0.01, cfg=_cfg())
    assert s2.iterations == 2
    assert s2.spent_usd == pytest.approx(0.18)
    assert fix_loop.load_state(work_item_id, tenant_id).spent_usd == pytest.approx(0.18)


def test_register_retry_refuses_after_iteration_cap_p6(work_item_id, tenant_id):
    state = _state(work_item_id, tenant_id, iterations=3)
    with pytest.raises(fix_loop.FixLoopBudgetExceeded):
        fix_loop.register_retry(state, coder_cost_usd=0.1, cfg=_cfg(max_retries=3))


def test_register_retry_refuses_after_budget_cap_p6(work_item_id, tenant_id):
    state = _state(work_item_id, tenant_id, iterations=1, spent=5.0)
    with pytest.raises(fix_loop.FixLoopBudgetExceeded):
        fix_loop.register_retry(state, coder_cost_usd=0.1, cfg=_cfg(max_retries=5, budget=5.0))


def test_escalate_marks_exhausted_durably(work_item_id, tenant_id):
    state = _state(work_item_id, tenant_id, iterations=3, spent=0.3)
    fix_loop.escalate_to_operator(state, reason="retries esgotados", objections=["z"])
    loaded = fix_loop.load_state(work_item_id, tenant_id)
    assert loaded.exhausted is True
    assert loaded.iterations == 3


def test_full_bounded_loop_reject_reject_reject_escalate(work_item_id, tenant_id):
    """Simula o loop end-to-end: 3 reprovações consecutivas -> escala.
    Cada iteração debita budget; a 4ª tentativa nunca acontece (P6)."""
    cfg = _cfg(max_retries=3)
    state = fix_loop.load_state(work_item_id, tenant_id)
    fail = L2Verdict(work_item_id=work_item_id, passed=False, objections=["persiste"])

    for _ in range(3):
        decision = fix_loop.decide_next_action(verdict=fail, state=state, cfg=cfg)
        assert decision.action == "retry_coder"
        state = fix_loop.register_retry(state, coder_cost_usd=0.1, l2_cost_usd=0.02, cfg=cfg)

    # após 3 retornos, a próxima decisão escala
    final = fix_loop.decide_next_action(verdict=fail, state=state, cfg=cfg)
    assert final.action == "escalate_operator"
    fix_loop.escalate_to_operator(state, reason=final.reason)

    loaded = fix_loop.load_state(work_item_id, tenant_id)
    assert loaded.iterations == 3
    assert loaded.exhausted is True
    assert loaded.spent_usd == pytest.approx(0.36)
