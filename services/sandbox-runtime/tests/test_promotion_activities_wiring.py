"""WSC-E4-T3: proves that `eval_skill_candidate` and `promote_skill` are real
Temporal Python SDK Activities (registered via `@activity.defn(name=...)`) and
that the contract (`dse_contracts`) crosses the Activity boundary intact.

Uses `temporalio.testing.ActivityEnvironment` (the SDK's official harness, not a
mock) against real Postgres — same discipline as
`test_temporal_activity_wiring.py`. Monkeypatches the `skill_promotion` module's
DSN to the test role (dse), since the production Activities open their own
connection (they do not receive `conn`).
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from temporalio.testing import ActivityEnvironment

from dse_contracts import (
    ACTIVITY_EVAL_SKILL_CANDIDATE,
    ACTIVITY_PROMOTE_SKILL,
    EvalSkillCandidateInput,
    PromoteSkillInput,
)
from sandbox_runtime import skill_promotion as sp
from sandbox_runtime.activities import eval_skill_candidate, promote_skill

HUMAN = "principal:human:curator-ana"


def test_activity_names_match_the_contract():
    assert eval_skill_candidate.__temporal_activity_definition.name == ACTIVITY_EVAL_SKILL_CANDIDATE
    assert promote_skill.__temporal_activity_definition.name == ACTIVITY_PROMOTE_SKILL


@pytest.fixture()
def tenant(pg_conn, pg_dsn, monkeypatch):
    # The Activities open their own connection via sp._DSN — point it at the test role.
    monkeypatch.setattr(sp, "_DSN", pg_dsn)
    t = f"act-{uuid.uuid4().hex[:10]}"
    yield t
    with pg_conn, pg_conn.cursor() as cur:
        cur.execute("DELETE FROM skill_registry WHERE tenant_id = %s", (t,))
        cur.execute("DELETE FROM skill_episode WHERE tenant_id = %s", (t,))
        cur.execute("DELETE FROM skill_eval WHERE tenant_id = %s", (t,))


def _make_candidate(pg_conn, tenant, skill_key, version):
    with pg_conn, pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO skill_registry (tenant_id, skill_key, title, body, category, applies_to, "
            "status, created_by, version, pattern_key, provenance) "
            "VALUES (%s,%s,'t','b','auto','[\"default\"]'::jsonb,'candidate','system:skill-promotion',%s,%s,'{}'::jsonb)",
            (tenant, skill_key, version, f"pat-{skill_key}"),
        )


def test_eval_and_promote_through_real_activity_environment(pg_conn, tenant):
    env = ActivityEnvironment()
    key = "auto-wiring"
    _make_candidate(pg_conn, tenant, key, 1)
    # One episode with the candidate's pattern → a positive in the eval set derived from the DB.
    sp.record_episode(tenant, "ci_repair", f"pat-{key}", conn=pg_conn)

    eval_res = asyncio.run(
        env.run(
            eval_skill_candidate,
            EvalSkillCandidateInput(tenant_id=tenant, skill_key=key, candidate_version=1),
        )
    )
    assert eval_res.passed is True
    assert eval_res.negative_regressions == 0

    prom = asyncio.run(
        env.run(
            promote_skill,
            PromoteSkillInput(tenant_id=tenant, skill_key=key, version=1, to_status="approved", approver=HUMAN),
        )
    )
    assert prom.ok is True and prom.to_status == "approved"


def test_promote_activity_refuses_without_approver(pg_conn, tenant):
    """Adversarial through the real Activity: to_status=active without an
    approver PROPAGATES the refusal (ApproverRequired) — there is no silent
    ok=False return."""
    env = ActivityEnvironment()
    key = "auto-wiring-adv"
    _make_candidate(pg_conn, tenant, key, 1)
    with pytest.raises(sp.ApproverRequired):
        asyncio.run(
            env.run(
                promote_skill,
                PromoteSkillInput(tenant_id=tenant, skill_key=key, version=1, to_status="active", approver=None),
            )
        )
