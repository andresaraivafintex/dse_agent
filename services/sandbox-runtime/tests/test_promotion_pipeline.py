"""WSC-E4-T3 — esteira de promoção governada (EXIT da Fase 4).

Contra Postgres real. Cobre:
  - o caminho de saída COMPLETO: candidate → eval(pass) → approved (com
    approver) → canary → active → rollback demonstrado (o ponteiro volta à
    versão anterior E a skill some do Planner);
  - o teste ADVERSARIAL P1/P3: promote_skill(to_status=active/approved,
    approver=None|system:*) RECUSA por construção (levanta ApproverRequired);
  - gates estruturais: candidate→approved sem eval passante bloqueia
    (EvalGateNotPassed); transição fora da máquina levanta IllegalTransition;
  - o índice único parcial garante no máximo UMA versão servida por skill.
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
    """Registra um eval PASSANTE (injetado, determinístico) para destravar o
    gate candidate→approved."""
    cases = [sp.EvalCase(label="positive", pattern_key=f"pat-{skill_key}")]
    # o candidate materializado tem pattern_key = skill_key sem prefixo 'auto-';
    # aqui o candidate é criado direto no teste com pattern_key conhecido.
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
# EXIT test — fluxo completo + rollback restaura a versão anterior.
# ---------------------------------------------------------------------------
def test_full_pipeline_then_rollback_restores_previous(pg_conn, tenant):
    key = "auto-payments-guard"

    # --- v1 sobe a esteira inteira até active ---
    _make_candidate(pg_conn, tenant, key, 1, body="guidance v1")
    _pass_eval(pg_conn, tenant, key, 1)
    sp.promote(tenant, key, 1, "approved", approver=HUMAN, conn=pg_conn)
    sp.promote(tenant, key, 1, "canary", conn=pg_conn)   # canary = shadow, não servido
    served_canary = _served(pg_conn, tenant)
    assert key not in served_canary, "canary NÃO é servido ao Planner de produção"
    sp.promote(tenant, key, 1, "active", approver=HUMAN, conn=pg_conn)

    served = _served(pg_conn, tenant)
    assert key in served and served[key].body == "guidance v1"

    # --- v2 sobe e SUPLANTA a v1 (rebaixa a servida anterior ao ENTRAR no
    # conjunto servido, i.e. no passo 'approved' — o índice único parcial
    # nunca deixa duas versões servidas coexistirem). ---
    _make_candidate(pg_conn, tenant, key, 2, body="guidance v2")
    _pass_eval(pg_conn, tenant, key, 2)
    out = sp.promote(tenant, key, 2, "approved", approver=HUMAN, conn=pg_conn)
    assert out.superseded_version == 1, "v1 rebaixada ao servir a v2"
    sp.promote(tenant, key, 2, "canary", conn=pg_conn)
    sp.promote(tenant, key, 2, "active", approver=HUMAN, conn=pg_conn)

    served = _served(pg_conn, tenant)
    assert key in served and served[key].body == "guidance v2", "Planner agora vê a v2"
    # invariante estrutural: só UMA versão servida da skill.
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM skill_registry "
            "WHERE tenant_id=%s AND skill_key=%s AND status IN ('approved','active')",
            (tenant, key),
        )
        assert cur.fetchone()[0] == 1

    # --- ROLLBACK: v2 active → rolled_back; o ponteiro volta para a v1 ---
    roll = sp.promote(tenant, key, 2, "rolled_back", reason="regressão em prod", conn=pg_conn)
    assert roll.restored_version == 1

    served = _served(pg_conn, tenant)
    assert key in served and served[key].body == "guidance v1", "rollback devolveu o ponteiro à v1"

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT version, status FROM skill_registry "
            "WHERE tenant_id=%s AND skill_key=%s ORDER BY version",
            (tenant, key),
        )
        rows = dict(cur.fetchall())
    assert rows == {1: "active", 2: "rolled_back"}


def test_rollback_without_previous_removes_from_planner(pg_conn, tenant):
    """Rollback de uma skill sem versão anterior: ela simplesmente SOME do
    Planner (failure mode 13, caso base)."""
    key = "auto-solo"
    _make_candidate(pg_conn, tenant, key, 1, body="only version")
    _pass_eval(pg_conn, tenant, key, 1)
    sp.promote(tenant, key, 1, "approved", approver=HUMAN, conn=pg_conn)
    sp.promote(tenant, key, 1, "canary", conn=pg_conn)
    sp.promote(tenant, key, 1, "active", approver=HUMAN, conn=pg_conn)
    assert key in _served(pg_conn, tenant)

    roll = sp.promote(tenant, key, 1, "rolled_back", conn=pg_conn)
    assert roll.restored_version is None
    assert key not in _served(pg_conn, tenant), "após rollback a skill some do Planner"


# ---------------------------------------------------------------------------
# ADVERSARIAL — P1/P3 não-negociável: sem aprovador humano, é impossível.
# ---------------------------------------------------------------------------
def test_promote_to_active_without_approver_refuses(pg_conn, tenant):
    key = "auto-adv"
    _make_candidate(pg_conn, tenant, key, 1, body="x")
    _pass_eval(pg_conn, tenant, key, 1)
    sp.promote(tenant, key, 1, "approved", approver=HUMAN, conn=pg_conn)
    sp.promote(tenant, key, 1, "canary", conn=pg_conn)

    with pytest.raises(sp.ApproverRequired):
        sp.promote(tenant, key, 1, "active", approver=None, conn=pg_conn)
    # e a skill NÃO foi ativada (o estado não mudou).
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
    """P3: nenhuma skill se auto-promove — um ator system:* não é aprovador."""
    key = "auto-adv3"
    _make_candidate(pg_conn, tenant, key, 1, body="x")
    _pass_eval(pg_conn, tenant, key, 1)
    with pytest.raises(sp.ApproverRequired):
        sp.promote(tenant, key, 1, "approved", approver="system:skill-promotion", conn=pg_conn)


def test_candidate_to_approved_blocked_without_passing_eval(pg_conn, tenant):
    key = "auto-noeval"
    _make_candidate(pg_conn, tenant, key, 1, body="x")
    # sem eval registrado → gate bloqueia por construção, mesmo com aprovador.
    with pytest.raises(sp.EvalGateNotPassed):
        sp.promote(tenant, key, 1, "approved", approver=HUMAN, conn=pg_conn)


def test_illegal_transition_refused(pg_conn, tenant):
    key = "auto-illegal"
    _make_candidate(pg_conn, tenant, key, 1, body="x")
    _pass_eval(pg_conn, tenant, key, 1)
    # candidate → active direto não existe na máquina de estados.
    with pytest.raises(sp.IllegalTransition):
        sp.promote(tenant, key, 1, "active", approver=HUMAN, conn=pg_conn)
