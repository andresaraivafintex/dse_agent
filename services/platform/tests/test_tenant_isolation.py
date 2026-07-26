"""WSF-E4-T3 — tenant isolation verification suite (NFR-03), layer by layer,
with ACTIVE cross-tenant access attempts that MUST fail and be audited.

Layers exercised against the real Postgres:
  - queues (fairness keys)      — deterministic namespacing, no collisions
  - artifacts (prefixes)        — per-tenant prefix, path traversal rejected
  - skills (WS-C skill_registry)— A's Planner cannot load B's skill
  - retrieval (WS-C E5)         — A's query cannot read B's document
  - audit (partitions)          — A's query cannot read B's audit
  - tokens (WS-D virtual_keys)  — A cannot present B's key

Each cross-tenant attempt is checked on two axes: (1) it raises
`CrossTenantViolation` (fail-closed), (2) it left a `cross_tenant_access_denied`
row in the audit (P8).

The skills/retrieval/tokens layers depend on tables owned by OTHER workstreams
(WS-C `skill_registry`/`retrieval_documents`, WS-D `virtual_keys`). If one does
not exist (that workstream has not applied its migration yet), the corresponding
test SKIPS with a clear reason instead of failing — same pattern as the Phase 1
egress-proxy adversarial tests.
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
# Layer: queues (fairness keys)
# ---------------------------------------------------------------------------
def test_fairness_keys_never_collide(tenants):
    a, b = tenants
    assert fairness_key(a) != fairness_key(b)
    assert fairness_key(a).startswith("tenant::")


def test_fairness_key_rejects_malformed_tenant():
    with pytest.raises(ValueError):
        fairness_key("evil::tenant")  # injected separator
    with pytest.raises(ValueError):
        fairness_key("")


# ---------------------------------------------------------------------------
# Layer: artifacts (per-tenant prefixes)
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
# Layer: central guard (unit)
# ---------------------------------------------------------------------------
def test_guard_same_tenant_allows_and_denies(tenants):
    a, b = tenants
    # same tenant: passes, no audit
    guard_same_tenant(requesting_tenant=a, resource_tenant=a, layer="x", resource_ref="r1")
    # different tenant: raises + audits
    with pytest.raises(CrossTenantViolation):
        guard_same_tenant(requesting_tenant=a, resource_tenant=b, layer="x", resource_ref="r2")
    assert _cross_tenant_denials(a, "x") >= 1
    # nonexistent resource (None): blocks too (does not leak existence)
    with pytest.raises(CrossTenantViolation):
        guard_same_tenant(requesting_tenant=a, resource_tenant=None, layer="x", resource_ref="r3")


# ---------------------------------------------------------------------------
# Layer: skills (WS-C skill_registry) — active attack
# ---------------------------------------------------------------------------
def test_planner_cannot_load_other_tenant_skill(tenants):
    if not _table_exists("skill_registry"):
        pytest.skip("skill_registry (WS-C) not migrated yet")
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

    # tenant A tries to load tenant B's skill -> denied + audited
    with pytest.raises(CrossTenantViolation):
        fetch_skill_scoped(a, skill_id)
    assert _cross_tenant_denials(a, "skill") >= 1

    # the owner (B) can read its own skill
    got = fetch_skill_scoped(b, skill_id)
    assert got["tenant_id"] == b


# ---------------------------------------------------------------------------
# Layer: retrieval index (WS-C E5 retrieval_documents) — active attack
# ---------------------------------------------------------------------------
def test_retrieval_query_scoped_to_tenant(tenants):
    if not _table_exists("retrieval_documents"):
        pytest.skip("retrieval_documents (WS-C E5) not migrated yet")
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
# Layer: audit (per-tenant partitions) — active attack
# ---------------------------------------------------------------------------
def test_audit_query_cannot_cross_tenant(tenants):
    a, b = tenants
    # write one audit row for each tenant
    from dse_audit import emit

    emit(actor="system:test", action="probe", tenant_id=a, details={})
    emit(actor="system:test", action="probe", tenant_id=b, details={})

    # A querying its own audit: ok
    rows = query_audit_scoped(a, a)
    assert any(r["action"] == "probe" for r in rows)

    # A trying to query B's audit: denied + audited
    with pytest.raises(CrossTenantViolation):
        query_audit_scoped(a, b)
    assert _cross_tenant_denials(a, "audit") >= 1


# ---------------------------------------------------------------------------
# Layer: tokens (WS-D virtual_keys) — active attack
# ---------------------------------------------------------------------------
def test_token_belongs_to_tenant(tenants):
    if not _table_exists("virtual_keys"):
        pytest.skip("virtual_keys (WS-D) not migrated yet")
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

    # B (the owner) validates fine
    assert_token_belongs_to_tenant(b, alias)
    # A presenting B's key: denied + audited
    with pytest.raises(CrossTenantViolation):
        assert_token_belongs_to_tenant(a, alias)
    assert _cross_tenant_denials(a, "token") >= 1
