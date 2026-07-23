"""WSC-E4-T1: skill registry bootstrap + leitura consumida pelo Planner.

Contra Postgres real (migração 0010_wsc2 aplicada + seed). Prova:
  - só skills `status='approved'` são lidas (rascunho `draft-not-ready` nunca);
  - isolamento por tenant rigoroso (dev não vê acme-bank e vice-versa),
    inclusive para um tenant recém-criado dinamicamente no teste;
  - o filtro por task_class respeita `applies_to`/'default'.
"""
from __future__ import annotations

import uuid


from sandbox_runtime.skill_registry import Skill, read_approved_skills


def test_reads_only_approved_skills_for_tenant(pg_conn):
    keys = {s.skill_key for s in read_approved_skills("dev", conn=pg_conn)}
    assert "draft-not-ready" not in keys, "rascunho (draft) não deve ser lido pelo Planner"
    assert {"pci-dss-logging", "migrations-reversible", "idempotent-webhooks"} <= keys


def test_tenant_isolation_seeded(pg_conn):
    dev = {s.skill_key for s in read_approved_skills("dev", conn=pg_conn)}
    acme = {s.skill_key for s in read_approved_skills("acme-bank", conn=pg_conn)}
    assert "acme-naming" not in dev, "skill de acme-bank vazou para dev"
    assert "pci-dss-logging" not in acme, "skill de dev vazou para acme-bank"
    assert acme == {"acme-naming"}


def test_tenant_isolation_dynamic(pg_conn):
    """Cria dois tenants novos com skills homônimas e prova que a leitura de um
    nunca traz a do outro."""
    t_a = f"iso-a-{uuid.uuid4().hex[:8]}"
    t_b = f"iso-b-{uuid.uuid4().hex[:8]}"
    with pg_conn, pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO skill_registry (tenant_id, skill_key, title, body, category, created_by, status) "
            "VALUES (%s,'shared-key','A title','A body','general','principal:human:t','approved'),"
            "       (%s,'shared-key','B title','B body','general','principal:human:t','approved')",
            (t_a, t_b),
        )
    try:
        a = read_approved_skills(t_a, conn=pg_conn)
        b = read_approved_skills(t_b, conn=pg_conn)
        assert len(a) == 1 and a[0].body == "A body"
        assert len(b) == 1 and b[0].body == "B body"
    finally:
        with pg_conn, pg_conn.cursor() as cur:
            cur.execute("DELETE FROM skill_registry WHERE tenant_id IN (%s,%s)", (t_a, t_b))


def test_task_class_filter(pg_conn):
    # pci-dss-logging aplica-se a ["payments","default"] → aparece para payments.
    pay = {s.skill_key for s in read_approved_skills("dev", task_class="payments", conn=pg_conn)}
    assert "pci-dss-logging" in pay


def test_skill_renders_context_block(pg_conn):
    skills = read_approved_skills("dev", conn=pg_conn)
    block = skills[0].as_context_block()
    assert block.startswith("### skill:")
    assert isinstance(skills[0], Skill)
