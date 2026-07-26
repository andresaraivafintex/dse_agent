"""WSA-E1-T5 — Platform -> tenant mapping.

Resolves the `tenant_id` from a platform key (Slack workspace `team_id`,
GitHub App installation `installation_id`, Jira site `cloud_id`/base_url) by
querying `tenant_platform_bindings` (migrations/0008_wsa2.sql).

DOCUMENTED fallback (P6 decline-never-truncate, but here it is
decline-to-the-single-tenant-default, not truncate): missing binding ->
`DSE_TENANT_ID` (env, default `tenant_dev`) WITH a warning audit row
(`action="tenant_binding_missing_fallback_default"`) — it never guesses a
tenant, it always leaves evidence that it fell back to the default. Once the
real mapping exists (multi-tenant, §2.3 of the Phase 2 addendum), populating
`tenant_platform_bindings` stops the resolution from falling back, with no code
change in the adapters.

Stable signature `(conn, platform, binding_key) -> ResolvedTenant` so WS-F can
swap the source (identity map / ADR-22) without breaking the adapters.
"""
from __future__ import annotations

import os
from typing import NamedTuple

from dse_audit import emit as audit_emit


class ResolvedTenant(NamedTuple):
    tenant_id: str
    from_binding: bool  # True if it came from tenant_platform_bindings; False if default fallback


def default_tenant() -> str:
    """Single-tenant fallback tenant (same env var the Phase 1 adapters already
    used in `config.get_tenant_id`)."""
    return os.environ.get("DSE_TENANT_ID", "tenant_dev")


def resolve_tenant(
    conn,
    *,
    platform: str,
    binding_key: str | None,
    audit_on_fallback: bool = True,
) -> ResolvedTenant:
    """Resolves the tenant of a platform event.

    A missing/None `binding_key` (e.g. a webhook that does not carry the
    workspace/installation/site identifier) OR no matching row in
    `tenant_platform_bindings` -> falls back to the default `DSE_TENANT_ID`,
    emitting a warning audit row (unless `audit_on_fallback=False`, used when
    the caller is going to emit its own context anyway).

    Transaction note: it uses `conn` for the read and for the fallback audit
    row but does NOT commit — it follows the `dse_audit.emit(conn=...)`
    convention, the caller owns the transaction boundary.
    """
    fallback = default_tenant()

    if binding_key:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tenant_id FROM tenant_platform_bindings WHERE platform = %s AND binding_key = %s",
                (platform, str(binding_key)),
            )
            row = cur.fetchone()
        if row is not None:
            return ResolvedTenant(row[0], True)

    if audit_on_fallback:
        audit_emit(
            actor=f"system:adapter-{platform}",
            action="tenant_binding_missing_fallback_default",
            tenant_id=fallback,
            details={"platform": platform, "binding_key": binding_key, "fallback_tenant_id": fallback},
            conn=conn,
        )
    return ResolvedTenant(fallback, False)
