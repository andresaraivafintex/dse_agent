"""WSC-E4-T2 — captura de episódios e materialização de skill candidates.

Contra Postgres real (migrações 0019 + 0020 aplicadas). Prova:
  - as 3 sources gravam episódios em skill_episode;
  - materialização é DETERMINÍSTICA por limiar (config, não LLM) — só dispara
    quando occurrences >= threshold;
  - o candidate nasce status='candidate' com version incrementada + proveniência,
    e NÃO é servido ao Planner (só {approved,active} são);
  - materialização é idempotente (não duplica candidates);
  - o eval do candidate produz score/contagens e grava em skill_eval; um caso
    negativo que dispara conta como negative_regression e reprova (passed=False).
"""
from __future__ import annotations

import uuid

import pytest

from sandbox_runtime import skill_promotion as sp
from sandbox_runtime.skill_registry import read_approved_skills


@pytest.fixture()
def tenant(pg_conn):
    t = f"promo-{uuid.uuid4().hex[:10]}"
    yield t
    with pg_conn, pg_conn.cursor() as cur:
        cur.execute("DELETE FROM skill_registry WHERE tenant_id = %s", (t,))
        cur.execute("DELETE FROM skill_episode WHERE tenant_id = %s", (t,))
        cur.execute("DELETE FROM skill_eval WHERE tenant_id = %s", (t,))


def test_record_episode_all_three_sources(pg_conn, tenant):
    for src in ("clarification", "ci_repair", "review_feedback"):
        sp.record_episode(tenant, src, "pat-x", work_item_id="wi-1", conn=pg_conn)
    counts = sp.pattern_occurrence_counts(tenant, conn=pg_conn)
    assert counts["pat-x"] == 3


def test_record_episode_rejects_bad_source(pg_conn, tenant):
    with pytest.raises(ValueError):
        sp.record_episode(tenant, "not_a_source", "pat-x", conn=pg_conn)


def test_materialize_below_threshold_is_noop(pg_conn, tenant):
    sp.record_episode(tenant, "ci_repair", "pat-low", conn=pg_conn)
    sp.record_episode(tenant, "ci_repair", "pat-low", conn=pg_conn)
    created = sp.materialize_candidates(tenant, threshold=3, conn=pg_conn)
    assert created == []


def test_materialize_creates_candidate_at_threshold(pg_conn, tenant):
    for _ in range(3):
        sp.record_episode(tenant, "review_feedback", "pat-hit", work_item_id="wi-9", conn=pg_conn)
    created = sp.materialize_candidates(tenant, threshold=3, conn=pg_conn)
    assert len(created) == 1
    c = created[0]
    assert c.skill_key == "auto-pat-hit"
    assert c.version == 1
    assert c.pattern_key == "pat-hit"

    # nasce como candidate, com proveniência e version — e NÃO é servido ao Planner.
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT status, version, pattern_key, provenance->>'materialized_from' "
            "FROM skill_registry WHERE tenant_id=%s AND skill_key=%s",
            (tenant, "auto-pat-hit"),
        )
        status, version, pat, prov_src = cur.fetchone()
    assert status == "candidate"
    assert version == 1
    assert pat == "pat-hit"
    assert prov_src == "skill_episode"

    served = {s.skill_key for s in read_approved_skills(tenant, conn=pg_conn)}
    assert "auto-pat-hit" not in served, "candidate NÃO pode ser servido ao Planner"


def test_materialize_is_idempotent(pg_conn, tenant):
    for _ in range(4):
        sp.record_episode(tenant, "clarification", "pat-idem", conn=pg_conn)
    first = sp.materialize_candidates(tenant, threshold=3, conn=pg_conn)
    second = sp.materialize_candidates(tenant, threshold=3, conn=pg_conn)
    assert len(first) == 1
    assert second == [], "não deve re-materializar um candidate já vivo"
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM skill_registry WHERE tenant_id=%s AND skill_key=%s",
            (tenant, "auto-pat-idem"),
        )
        assert cur.fetchone()[0] == 1


def test_eval_from_episodes_passes_when_no_negative_regression(pg_conn, tenant):
    # pattern alvo (positivos) + um pattern diferente (negativos): a skill de
    # 'pat-target' NÃO dispara nos episódios de 'pat-other' → 0 regressões.
    for _ in range(3):
        sp.record_episode(tenant, "ci_repair", "pat-target", conn=pg_conn)
    sp.record_episode(tenant, "ci_repair", "pat-other", conn=pg_conn)
    sp.materialize_candidates(tenant, threshold=3, conn=pg_conn)

    outcome = sp.evaluate_candidate(tenant, "auto-pat-target", 1, conn=pg_conn)
    assert outcome.negative_regressions == 0
    assert outcome.positive_hits >= 1
    assert outcome.passed is True

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT passed, negative_regressions FROM skill_eval "
            "WHERE tenant_id=%s AND skill_key=%s AND candidate_version=1",
            (tenant, "auto-pat-target"),
        )
        row = cur.fetchone()
    assert row == (True, 0), "trilha de eval gravada em skill_eval"


def test_eval_fails_on_negative_regression(pg_conn, tenant):
    for _ in range(3):
        sp.record_episode(tenant, "ci_repair", "pat-reg", conn=pg_conn)
    sp.materialize_candidates(tenant, threshold=3, conn=pg_conn)

    # eval set injetado com um NEGATIVO que compartilha o pattern_key do
    # candidate → a skill dispara onde não devia → regressão → reprova.
    cases = [
        sp.EvalCase(label="positive", pattern_key="pat-reg"),
        sp.EvalCase(label="negative", pattern_key="pat-reg"),
    ]
    outcome = sp.evaluate_candidate(
        tenant, "auto-pat-reg", 1, conn=pg_conn, eval_cases=cases
    )
    assert outcome.negative_regressions == 1
    assert outcome.passed is False
