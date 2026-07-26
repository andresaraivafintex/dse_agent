"""WSF-E4-T3 — multi-tenant isolation enforcement (NFR-03), layer by layer.

This module is the *reusable primitive* for "every read is checked against the
tenant boundary of whoever asked"; the *proof* (active cross-tenant access
attempts that MUST fail and be audited) lives in the `tests/test_tenant_isolation.py`
suite.

Layers covered (NFR-03):
  - queues       -> `fairness_key(tenant)` (deterministic namespacing of the
                    worker-side fairness key WS-B reads from `tenant_config`)
  - artifacts    -> `artifact_prefix(tenant)` (per-tenant prefix in the store)
  - skills       -> `fetch_skill_scoped` (WS-C skill_registry, tenant-scoped)
  - retrieval    -> `query_retrieval_scoped` (WS-C retrieval_documents, E5)
  - audit        -> `query_audit_scoped` (per-tenant audit_log partitions)
  - tokens       -> `assert_token_belongs_to_tenant` (WS-D virtual_keys)

Every attempt by tenant A to read a resource of tenant B is DENIED and AUDITED as
`cross_tenant_access_denied` (P8). The central guard `guard_same_tenant` is the
structural "missing/wrong blocks" — it never returns another tenant's data, and
never fails silently (P6).
"""
from __future__ import annotations

import os
import re

import psycopg2
import psycopg2.extras
from dse_audit import emit

_DSN = os.environ.get(
    "DSE_PLATFORM_DATABASE_URL",
    os.environ.get("DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"),
)

_SAFE_TENANT = re.compile(r"^[A-Za-z0-9._-]+$")


def _get_connection():
    return psycopg2.connect(_DSN)


class CrossTenantViolation(Exception):
    """A tenant tried to access another tenant's resource. Fail-closed (P6)."""


def _valid_tenant(tenant_id: str) -> bool:
    return bool(tenant_id) and bool(_SAFE_TENANT.match(tenant_id))


def guard_same_tenant(
    *,
    requesting_tenant: str,
    resource_tenant: str | None,
    layer: str,
    resource_ref: str,
    actor: str = "system:tenant-isolation",
    conn=None,
) -> None:
    """Enforcement core: raises `CrossTenantViolation` (and audits) if
    `resource_tenant` is None (nonexistent/hidden resource) or differs from
    `requesting_tenant`. Called by every scoped accessor below."""
    if resource_tenant is not None and resource_tenant == requesting_tenant:
        return
    emit(
        actor=actor,
        action="cross_tenant_access_denied",
        tenant_id=requesting_tenant,
        details={
            "layer": layer,
            "resource_ref": resource_ref,
            "resource_tenant": resource_tenant,
            "requesting_tenant": requesting_tenant,
        },
        conn=conn,
    )
    raise CrossTenantViolation(
        f"tenant {requesting_tenant!r} tried to access {layer} {resource_ref!r} "
        f"do tenant {resource_tenant!r}"
    )


# ---------------------------------------------------------------------------
# Layer: queues (fairness keys) — deterministic namespacing
# ---------------------------------------------------------------------------
def fairness_key(tenant_id: str) -> str:
    """Worker-side fairness key (WS-B reads caps from `tenant_config` by this
    key). Strong per-tenant namespacing: two tenants NEVER collide. Rejects a
    malformed tenant_id (defense against separator injection)."""
    if not _valid_tenant(tenant_id):
        raise ValueError(f"invalid tenant_id for fairness_key: {tenant_id!r}")
    return f"tenant::{tenant_id}"


# ---------------------------------------------------------------------------
# Layer: artifacts — per-tenant prefix
# ---------------------------------------------------------------------------
def artifact_prefix(tenant_id: str) -> str:
    """Mandatory prefix for any artifact/object key of a tenant. Rejects a
    malformed tenant_id (prevents `../` path traversal in the prefix)."""
    if not _valid_tenant(tenant_id):
        raise ValueError(f"invalid tenant_id for artifact_prefix: {tenant_id!r}")
    return f"tenants/{tenant_id}/"


def artifact_key(tenant_id: str, relative_path: str) -> str:
    if ".." in relative_path or relative_path.startswith("/"):
        raise ValueError(f"unsafe artifact path: {relative_path!r}")
    return artifact_prefix(tenant_id) + relative_path.lstrip("/")


# ---------------------------------------------------------------------------
# Layer: skills (WS-C skill_registry) — tenant-scoped
# ---------------------------------------------------------------------------
def fetch_skill_scoped(requesting_tenant: str, skill_id: int, *, conn=None):
    """Fetches a skill by id, but ONLY returns it if it belongs to the requesting
    tenant. If the skill belongs to another tenant, it audits
    `cross_tenant_access_denied` and raises `CrossTenantViolation` — it never
    returns the row (one tenant's Planner must not load another's skill)."""
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM skill_registry WHERE id = %s", (skill_id,))
            row = cur.fetchone()
        resource_tenant = row["tenant_id"] if row else None
        guard_same_tenant(
            requesting_tenant=requesting_tenant,
            resource_tenant=resource_tenant,
            layer="skill",
            resource_ref=str(skill_id),
            conn=conn,
        )
        if owns:
            conn.commit()  # persist the violation audit (not reached if it raised)
        return dict(row)
    except Exception:
        if owns:
            conn.commit()  # the violation audit was already emitted on this conn — preserve it
        raise
    finally:
        if owns:
            conn.close()


# ---------------------------------------------------------------------------
# Layer: retrieval index (WS-C E5 retrieval_documents) — tenant-scoped
# ---------------------------------------------------------------------------
def query_retrieval_scoped(requesting_tenant: str, document_id: int, *, conn=None):
    """Same as `fetch_skill_scoped`, for the retrieval index: a tenant cannot
    query another tenant's indexed document."""
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM retrieval_documents WHERE id = %s", (document_id,))
            row = cur.fetchone()
        resource_tenant = row["tenant_id"] if row else None
        guard_same_tenant(
            requesting_tenant=requesting_tenant,
            resource_tenant=resource_tenant,
            layer="retrieval",
            resource_ref=str(document_id),
            conn=conn,
        )
        if owns:
            conn.commit()
        return dict(row)
    except Exception:
        if owns:
            conn.commit()
        raise
    finally:
        if owns:
            conn.close()


# ---------------------------------------------------------------------------
# Layer: audit (per-tenant partitions) — no query crosses tenants
# ---------------------------------------------------------------------------
def query_audit_scoped(requesting_tenant: str, target_tenant: str, *, conn=None):
    """Per-tenant audit query. An attempt by one tenant to query ANOTHER tenant's
    audit is denied + audited. Only when requesting == target does the query run
    (returning the rows of that partition)."""
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        guard_same_tenant(
            requesting_tenant=requesting_tenant,
            resource_tenant=target_tenant,
            layer="audit",
            resource_ref=f"audit_log[{target_tenant}]",
            conn=conn,
        )
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT action, actor, ts FROM audit_log WHERE tenant_id = %s ORDER BY ts DESC, id DESC LIMIT 100",
                (requesting_tenant,),
            )
            rows = [dict(r) for r in cur.fetchall()]
        if owns:
            conn.commit()
        return rows
    except Exception:
        if owns:
            conn.commit()
        raise
    finally:
        if owns:
            conn.close()


# ---------------------------------------------------------------------------
# Layer: tokens (WS-D virtual_keys)
# ---------------------------------------------------------------------------
def assert_token_belongs_to_tenant(requesting_tenant: str, key_alias: str, *, conn=None) -> None:
    """Confirms the virtual key (by alias) belongs to the tenant presenting it.
    A tenant presenting another tenant's key alias is denied + audited."""
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT tenant_id FROM virtual_keys WHERE key_alias = %s", (key_alias,))
            row = cur.fetchone()
        resource_tenant = row[0] if row else None
        guard_same_tenant(
            requesting_tenant=requesting_tenant,
            resource_tenant=resource_tenant,
            layer="token",
            resource_ref=key_alias,
            conn=conn,
        )
        if owns:
            conn.commit()
    except Exception:
        if owns:
            conn.commit()
        raise
    finally:
        if owns:
            conn.close()
