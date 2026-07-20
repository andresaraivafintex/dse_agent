"""WSF-E4-T3 — Suíte de verificação de isolamento de tenant (NFR-03), camada a
camada, com TENTATIVAS ATIVAS de acesso cross-tenant que DEVEM falhar e ser
auditadas.

Camadas exercitadas contra o Postgres real:
  - filas (fairness keys)       — namespacing determinístico, sem colisão
  - artifacts (prefixos)        — prefixo por tenant, path traversal rejeitado
  - skills (skill_registry WS-C)— Planner de A não carrega skill de B
  - retrieval (WS-C E5)         — query de A não lê documento de B
  - audit (partições)           — query de A não lê audit de B
  - tokens (virtual_keys WS-D)  — A não apresenta key de B

Cada tentativa cross-tenant é verificada em dois eixos: (1) levanta
`CrossTenantViolation` (fail-closed), (2) deixou uma linha
`cross_tenant_access_denied` no audit (P8).

As camadas de skills/retrieval/tokens dependem de tabelas de OUTROS workstreams
(WS-C `skill_registry`/`retrieval_documents`, WS-D `virtual_keys`). Se alguma
não existir (workstream ainda não subiu a migração), o teste correspondente
SKIPA com razão clara em vez de falhar — mesmo padrão dos testes adversariais
do egress-proxy da Fase 1.
"""
from __future__ import annotations

import uuid

import pytest
from dse_audit.client import get_connection
from dse_platform import (
    CrossTenantViolation,
    artifact_key,
    artifact_prefix,
    assert_token_belongs_to_tenant,
    fairness_key,
    fetch_skill_scoped,
    query_audit_scoped,
    query_retrieval_scoped,
)
from dse_platform.tenant_isolation import guard_same_tenant


def _table_exists(name: str) -> bool:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (name,))
            return cur.fetchone()[0] is not None
    finally:
        conn.close()


def _cross_tenant_denials(tenant_id: str, layer: str) -> int:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM audit_log
                WHERE tenant_id = %s AND action = 'cross_tenant_access_denied'
                  AND details->>'layer' = %s
                """,
                (tenant_id, layer),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()


@pytest.fixture()
def tenants():
    suffix = uuid.uuid4().hex[:8]
    return (f"tenantA-{suffix}", f"tenantB-{suffix}")


# ---------------------------------------------------------------------------
# Camada: filas (fairness keys)
# ---------------------------------------------------------------------------
def test_fairness_keys_never_collide(tenants):
    a, b = tenants
    assert fairness_key(a) != fairness_key(b)
    assert fairness_key(a).startswith("tenant::")


def test_fairness_key_rejects_malformed_tenant():
    with pytest.raises(ValueError):
        fairness_key("evil::tenant")  # separador injetado
    with pytest.raises(ValueError):
        fairness_key("")


# ---------------------------------------------------------------------------
# Camada: artifacts (prefixos por tenant)
# ---------------------------------------------------------------------------
def test_artifact_prefix_per_tenant(tenants):
    a, b = tenants
    assert artifact_prefix(a) != artifact_prefix(b)
    assert artifact_key(a, "plan.json") == f"tenants/{a}/plan.json"


def test_artifact_key_rejects_path_traversal(tenants):
    a, _ = tenants
    with pytest.raises(ValueError):
        artifact_key(a, "../othertenant/secret")
    with pytest.raises(ValueError):
        artifact_prefix("../evil")


# ---------------------------------------------------------------------------
# Camada: guard central (unidade)
# ---------------------------------------------------------------------------
def test_guard_same_tenant_allows_and_denies(tenants):
    a, b = tenants
    # mesmo tenant: passa, sem audit
    guard_same_tenant(requesting_tenant=a, resource_tenant=a, layer="x", resource_ref="r1")
    # tenant diferente: levanta + audita
    with pytest.raises(CrossTenantViolation):
        guard_same_tenant(requesting_tenant=a, resource_tenant=b, layer="x", resource_ref="r2")
    assert _cross_tenant_denials(a, "x") >= 1
    # recurso inexistente (None): também bloqueia (não vaza existência)
    with pytest.raises(CrossTenantViolation):
        guard_same_tenant(requesting_tenant=a, resource_tenant=None, layer="x", resource_ref="r3")


# ---------------------------------------------------------------------------
# Camada: skills (skill_registry do WS-C) — ataque ativo
# ---------------------------------------------------------------------------
def test_planner_cannot_load_other_tenant_skill(tenants):
    if not _table_exists("skill_registry"):
        pytest.skip("skill_registry (WS-C) ainda não migrada")
    a, b = tenants
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO skill_registry (tenant_id, skill_key, title, body, category, status, created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (b, f"skill-{uuid.uuid4().hex[:6]}", "B secret skill", "body", "cat", "approved", "usr_seed"),
            )
            skill_id = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    # tenant A tenta carregar a skill do tenant B -> negado + auditado
    with pytest.raises(CrossTenantViolation):
        fetch_skill_scoped(a, skill_id)
    assert _cross_tenant_denials(a, "skill") >= 1

    # o dono (B) consegue ler a própria skill
    got = fetch_skill_scoped(b, skill_id)
    assert got["tenant_id"] == b


# ---------------------------------------------------------------------------
# Camada: retrieval index (retrieval_documents do WS-C E5) — ataque ativo
# ---------------------------------------------------------------------------
def test_retrieval_query_scoped_to_tenant(tenants):
    if not _table_exists("retrieval_documents"):
        pytest.skip("retrieval_documents (WS-C E5) ainda não migrada")
    a, b = tenants
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO retrieval_documents (tenant_id, repo, path, kind, content, content_sha) "
                "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                (b, "org/b-repo", "src/secret.py", "file", "top secret", uuid.uuid4().hex),
            )
            doc_id = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(CrossTenantViolation):
        query_retrieval_scoped(a, doc_id)
    assert _cross_tenant_denials(a, "retrieval") >= 1
    assert query_retrieval_scoped(b, doc_id)["tenant_id"] == b


# ---------------------------------------------------------------------------
# Camada: audit (partições por tenant) — ataque ativo
# ---------------------------------------------------------------------------
def test_audit_query_cannot_cross_tenant(tenants):
    a, b = tenants
    # gera uma linha de audit para cada tenant
    from dse_audit import emit

    emit(actor="system:test", action="probe", tenant_id=a, details={})
    emit(actor="system:test", action="probe", tenant_id=b, details={})

    # A consultando o próprio audit: ok
    rows = query_audit_scoped(a, a)
    assert any(r["action"] == "probe" for r in rows)

    # A tentando consultar o audit de B: negado + auditado
    with pytest.raises(CrossTenantViolation):
        query_audit_scoped(a, b)
    assert _cross_tenant_denials(a, "audit") >= 1


# ---------------------------------------------------------------------------
# Camada: tokens (virtual_keys do WS-D) — ataque ativo
# ---------------------------------------------------------------------------
def test_token_belongs_to_tenant(tenants):
    if not _table_exists("virtual_keys"):
        pytest.skip("virtual_keys (WS-D) ainda não migrada")
    a, b = tenants
    alias = f"vk-{uuid.uuid4().hex[:8]}"
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO virtual_keys (tenant_id, work_item_id, stage, key_alias, key_hash, key_prefix) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (b, f"wi-{uuid.uuid4().hex[:6]}", "coder", alias, uuid.uuid4().hex, "sk-xxx"),
            )
        conn.commit()
    finally:
        conn.close()

    # B (dono) valida ok
    assert_token_belongs_to_tenant(b, alias)
    # A apresentando a key de B: negado + auditado
    with pytest.raises(CrossTenantViolation):
        assert_token_belongs_to_tenant(a, alias)
    assert _cross_tenant_denials(a, "token") >= 1
