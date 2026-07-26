"""WSC-E4-T3 — governed promotion pipeline (Fase 4 EXIT).

Against real Postgres. Covers:
  - the COMPLETE exit path: candidate → eval(pass) → approved (with an
    approver) → canary → active → rollback demonstrated (the pointer goes back
    to the previous version AND the skill disappears from the Planner);
  - the ADVERSARIAL P1/P3 test: promote_skill(to_status=active/approved,
    approver=None|system:*) REFUSES by construction (raises ApproverRequired);
  - structural gates: candidate→approved without a passing eval is blocked
    (EvalGateNotPassed); a transition outside the machine raises
    IllegalTransition;
  - the partial unique index guarantees at most ONE served version per skill.
"""
from __future__ import annotations

import uuid

import pytest

from sandbox_runtime import skill_promotion as sp
from sandbox_runtime.skill_registry import read_approved_skills

HUMAN = "principal:human:curator-ana"


@pytest.fixture()
def tenant(pg_conn):
    t = f"exit-{uuid.uuid4().hex[:10]}"
    yield t
    with pg_conn, pg_conn.cursor() as cur:
        cur.execute("DELETE FROM skill_registry WHERE tenant_id = %s", (t,))
        cur.execute("DELETE FROM skill_episode WHERE tenant_id = %s", (t,))
        cur.execute("DELETE FROM skill_eval WHERE tenant_id = %s", (t,))


def _served(pg_conn, tenant):
    return {s.skill_key: s for s in read_approved_skills(tenant, conn=pg_conn)}


def _pass_eval(pg_conn, tenant, skill_key, version):
    """Records a PASSING eval (injected, deterministic) to unlock the
    candidate→approved gate."""
    cases = [sp.EvalCase(label="positive", pattern_key=f"pat-{skill_key}")]
    # the materialized candidate has pattern_key = skill_key without the 'auto-'
    # prefix; here the candidate is created directly in the test with a known
    # pattern_key.
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT pattern_key FROM skill_registry WHERE tenant_id=%s AND skill_key=%s AND version=%s",
            (tenant, skill_key, version),
        )
        pk = cur.fetchone()[0]
    cases = [sp.EvalCase(label="positive", pattern_key=pk)]
    out = sp.evaluate_candidate(tenant, skill_key, version, conn=pg_conn, eval_cases=cases)
    assert out.passed is True
    return out


def _make_candidate(pg_conn, tenant, skill_key, version, body):
    with pg_conn, pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO skill_registry
                (tenant_id, skill_key, title, body, category, applies_to,
                 status, created_by, version, pattern_key, provenance)
            VALUES (%s,%s,%s,%s,'auto','[\"default\"]'::jsonb,'candidate',
                    'system:skill-promotion', %s, %s, '{}'::jsonb)
            """,
            (tenant, skill_key, f"title {version}", body, version, f"pat-{skill_key}"),
        )


# ---------------------------------------------------------------------------
# EXIT test — full flow + rollback restores the previous version.
# ---------------------------------------------------------------------------
def test_full_pipeline_then_rollback_restores_previous(pg_conn, tenant):
    key = "auto-payments-guard"

    # --- v1 climbs the whole pipeline up to active ---
    _make_candidate(pg_conn, tenant, key, 1, body="guidance v1")
    _pass_eval(pg_conn, tenant, key, 1)
    sp.promote(tenant, key, 1, "approved", approver=HUMAN, conn=pg_conn)
    sp.promote(tenant, key, 1, "canary", conn=pg_conn)   # canary = shadow, not served
    served_canary = _served(pg_conn, tenant)
    assert key not in served_canary, "a canary is NOT served to the production Planner"
    sp.promote(tenant, key, 1, "active", approver=HUMAN, conn=pg_conn)

    served = _served(pg_conn, tenant)
    assert key in served and served[key].body == "guidance v1"

    # --- v2 climbs and SUPERSEDES v1 (it demotes the previously served version
    # upon ENTERING the served set, i.e. at the 'approved' step — the partial
    # unique index never lets two served versions coexist). ---
    _make_candidate(pg_conn, tenant, key, 2, body="guidance v2")
    _pass_eval(pg_conn, tenant, key, 2)
    out = sp.promote(tenant, key, 2, "approved", approver=HUMAN, conn=pg_conn)
    assert out.superseded_version == 1, "v1 rebaixada ao servir a v2"
    sp.promote(tenant, key, 2, "canary", conn=pg_conn)
    sp.promote(tenant, key, 2, "active", approver=HUMAN, conn=pg_conn)

    served = _served(pg_conn, tenant)
    assert key in served and served[key].body == "guidance v2", "the Planner now sees v2"
    # structural invariant: only ONE served version of the skill.
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM skill_registry "
            "WHERE tenant_id=%s AND skill_key=%s AND status IN ('approved','active')",
            (tenant, key),
        )
        assert cur.fetchone()[0] == 1

    # --- ROLLBACK: v2 active → rolled_back; the pointer goes back to v1 ---
    roll = sp.promote(tenant, key, 2, "rolled_back", reason="regression in prod", conn=pg_conn)
    assert roll.restored_version == 1

    served = _served(pg_conn, tenant)
    assert key in served and served[key].body == "guidance v1", "the rollback moved the pointer back to v1"

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT version, status FROM skill_registry "
            "WHERE tenant_id=%s AND skill_key=%s ORDER BY version",
            (tenant, key),
        )
        rows = dict(cur.fetchall())
    assert rows == {1: "active", 2: "rolled_back"}


def test_rollback_without_previous_removes_from_planner(pg_conn, tenant):
    """Rollback of a skill with no previous version: it simply DISAPPEARS from
    the Planner (failure mode 13, base case)."""
    key = "auto-solo"
    _make_candidate(pg_conn, tenant, key, 1, body="only version")
    _pass_eval(pg_conn, tenant, key, 1)
    sp.promote(tenant, key, 1, "approved", approver=HUMAN, conn=pg_conn)
    sp.promote(tenant, key, 1, "canary", conn=pg_conn)
    sp.promote(tenant, key, 1, "active", approver=HUMAN, conn=pg_conn)
    assert key in _served(pg_conn, tenant)

    roll = sp.promote(tenant, key, 1, "rolled_back", conn=pg_conn)
    assert roll.restored_version is None
    assert key not in _served(pg_conn, tenant), "after a rollback the skill disappears from the Planner"


# ---------------------------------------------------------------------------
# ADVERSARIAL — P1/P3 non-negotiable: without a human approver, it is impossible.
# ---------------------------------------------------------------------------
def test_promote_to_active_without_approver_refuses(pg_conn, tenant):
    key = "auto-adv"
    _make_candidate(pg_conn, tenant, key, 1, body="x")
    _pass_eval(pg_conn, tenant, key, 1)
    sp.promote(tenant, key, 1, "approved", approver=HUMAN, conn=pg_conn)
    sp.promote(tenant, key, 1, "canary", conn=pg_conn)

    with pytest.raises(sp.ApproverRequired):
        sp.promote(tenant, key, 1, "active", approver=None, conn=pg_conn)
    # and the skill was NOT activated (the state did not change).
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM skill_registry WHERE tenant_id=%s AND skill_key=%s AND version=1",
            (tenant, key),
        )
        assert cur.fetchone()[0] == "canary"


def test_promote_to_approved_without_approver_refuses(pg_conn, tenant):
    key = "auto-adv2"
    _make_candidate(pg_conn, tenant, key, 1, body="x")
    _pass_eval(pg_conn, tenant, key, 1)
    with pytest.raises(sp.ApproverRequired):
        sp.promote(tenant, key, 1, "approved", approver="", conn=pg_conn)


def test_system_actor_cannot_approve(pg_conn, tenant):
    """P3: no skill promotes itself — a system:* actor is not an approver."""
    key = "auto-adv3"
    _make_candidate(pg_conn, tenant, key, 1, body="x")
    _pass_eval(pg_conn, tenant, key, 1)
    with pytest.raises(sp.ApproverRequired):
        sp.promote(tenant, key, 1, "approved", approver="system:skill-promotion", conn=pg_conn)


def test_candidate_to_approved_blocked_without_passing_eval(pg_conn, tenant):
    key = "auto-noeval"
    _make_candidate(pg_conn, tenant, key, 1, body="x")
    # no eval recorded → the gate blocks by construction, even with an approver.
    with pytest.raises(sp.EvalGateNotPassed):
        sp.promote(tenant, key, 1, "approved", approver=HUMAN, conn=pg_conn)


def test_illegal_transition_refused(pg_conn, tenant):
    key = "auto-illegal"
    _make_candidate(pg_conn, tenant, key, 1, body="x")
    _pass_eval(pg_conn, tenant, key, 1)
    # candidate → active directly does not exist in the state machine.
    with pytest.raises(sp.IllegalTransition):
        sp.promote(tenant, key, 1, "active", approver=HUMAN, conn=pg_conn)
