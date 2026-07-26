"""WSA-E6-T2b — Steering policy on top of the REAL identity map.

`is_authorized_to_steer(tenant_id, principal_id)` decides whether a principal
may "steer" (inject new direction via comment/review_comment) a task in
flight.

History: Phase 1 (WSA-E6-T2a) shipped the explicit per-tenant allowlist
fallback (`tenant_steering_allowlist`, migrations/0002_wsa.sql) — "no row =
not authorized", never "anyone can steer". The SIGNATURE
`(tenant_id, principal_id) -> bool` was deliberately kept stable so that
Phase 4 could swap the resolution underneath without breaking callers
(`ingest_gateway.correlate`, adapters). This is that swap.

Phase 4 replaces the resolution with an *identity-map-backed* one: instead of
only checking membership in an allowlist, we resolve whether the principal has
the right **role** to direct that tenant's task, using the sources of truth
WS-F already delivered in Phase 2:

  1. `dse_console_identity` (WSF-E3-T3 / ADR-22) — the principal's console
     identity, with `roles` (RBAC) and the offboarding state (`active`,
     `expires_at`). A role in `STEERING_ROLES` authorizes; an
     offboarded/expired principal is DENIED even if they remain on any list
     (offboarding is the authority that overrides — WSF-E3-T3).
  2. `dse_access_bundle` (WSF-E3-T2) — the `designated_approvers` of the
     tenant's effective bundle are, by construction, the humans trusted to
     approve/direct that tenant; being on the list is the "approver role"
     resolved via the bundle.

DOCUMENTED fallback (while the identity map does not resolve a role): the
explicit Phase 1 allowlist (`tenant_steering_allowlist`), which in practice is
seeded with the requester + the tenant's trusted CODEOWNERS-equivalents. This
preserves the Phase 1 behavior and tests exactly, and covers tenants that do
not yet have a console identity / access bundle configured.

Invariants preserved (Phase 1):
  - **Deny by default (P1/deny-by-default)**: none of the sources resolving a
    role/membership => `False`. Never "anyone can steer".
  - **Stable signature**: `(tenant_id, principal_id) -> bool`, with no `conn`
    in the public contract (it opens its own connection, like
    `resolve_principal`).
  - **Unauthorized does not signal + audit**: the rejection audit row
    (`steering_rejected_unauthorized`) and the "does not become a signal" part
    remain the responsibility of `ingest_gateway.correlate` (Phase 1
    invariant) — this module only returns the boolean. On the POSITIVE path we
    emit a provenance row (`steering_authorized`, with the resolution METHOD)
    to give evidence (P8) of HOW the authorization was granted.

Everything here is a deterministic set/string comparison (P1) — no LLM decides
who may steer.
"""
from __future__ import annotations

import json
import logging

from dse_audit import emit as audit_emit

from .db import get_connection

logger = logging.getLogger("ingest_gateway.steering")

# Roles (in dse_console_identity.roles) that authorize steering a task.
# Conservative set (P7 boring-first): the humans who operate/approve/maintain.
# WS-F administers the assignment of these roles in the console (WSF-E3-T3);
# here we only compare sets.
STEERING_ROLES: frozenset[str] = frozenset(
    {"operator", "approver", "steerer", "maintainer", "platform_admin", "admin"}
)

# Resolution methods (recorded in the provenance audit — P8).
_METHOD_ROLE_CONSOLE = "role_console"      # RBAC role resolved via dse_console_identity
_METHOD_ROLE_BUNDLE = "role_bundle"        # designated_approver of the tenant's access bundle
_METHOD_ALLOWLIST = "allowlist_fallback"   # documented fallback (requester + CODEOWNERS seed)


def is_authorized_to_steer(tenant_id: str, principal_id: str) -> bool:
    """True iff the principal has a role to direct a task of this tenant.

    Resolution order (deterministic; denies by default):
      0. Offboarding is the authority that overrides — a principal with an
         inactive/expired console row is denied, even if they remain on the
         allowlist/bundle.
      1. RBAC role via console identity (tenant-scoped: the principal's home
         tenant == tenant OR the principal is a platform operator with a NULL
         tenant).
      2. Approver role via the tenant's effective access bundle.
      3. Documented fallback: the explicit Phase 1 allowlist.
    """
    if not tenant_id or not principal_id:
        return False

    conn = get_connection()
    try:
        console = _load_console_identity(conn, principal_id)

        # 0. Offboarding overrides everything (WSF-E3-T3): the row exists and is
        #    deactivated/expired => denied, regardless of allowlist/bundle.
        if console is not None and not console["effective_active"]:
            return False

        method = _resolve_method(conn, tenant_id, principal_id, console)
        if method is None:
            return False

        # P8: provenance of the GRANT (the rejection is still audited by
        # correlate, Phase 1 invariant). Best-effort — never lets an audit
        # failure take down the authorization of a legitimate steer, but logs it.
        try:
            audit_emit(
                actor=principal_id,
                action="steering_authorized",
                tenant_id=tenant_id,
                details={"method": method},
            )
        except Exception:  # noqa: BLE001 — best-effort audit on the grant path
            logger.warning(
                "failed to emit the steering_authorized audit row (tenant=%s principal=%s method=%s)",
                tenant_id, principal_id, method, exc_info=True,
            )
        return True
    finally:
        conn.close()


def _resolve_method(conn, tenant_id: str, principal_id: str, console: dict | None) -> str | None:
    """Returns the method that authorizes, or None if no source resolves a role."""
    # 1. RBAC role via console identity, scoped to the tenant.
    if console is not None and (set(console["roles"]) & STEERING_ROLES):
        home = console["tenant_id"]
        if home is None or home == tenant_id:
            return _METHOD_ROLE_CONSOLE

    # 2. Approver role via the tenant's effective access bundle (default channel).
    if _is_designated_approver(conn, tenant_id, principal_id):
        return _METHOD_ROLE_BUNDLE

    # 3. Documented fallback: the explicit Phase 1 allowlist.
    if _in_steering_allowlist(conn, tenant_id, principal_id):
        return _METHOD_ALLOWLIST

    return None


def _load_console_identity(conn, principal_id: str) -> dict | None:
    """Reads the principal's console identity, if it exists. `effective_active`
    already combines `active` with the contractor expiration (ADR-22). `roles`
    is always a list (JSONB), `tenant_id` is the home tenant (None =
    multi-tenant platform operator)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT roles,
                   tenant_id,
                   (active AND (expires_at IS NULL OR expires_at > now())) AS effective_active
            FROM dse_console_identity
            WHERE principal_id = %s
            """,
            (principal_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    roles, home_tenant, effective_active = row
    return {
        "roles": list(roles or []),
        "tenant_id": home_tenant,
        "effective_active": bool(effective_active),
    }


def _is_designated_approver(conn, tenant_id: str, principal_id: str) -> bool:
    """True iff the principal is in the `designated_approvers` of the tenant's
    EFFECTIVE access bundle (default channel = NULL). Minimal, deterministic
    resolution of the enabled default bundle — steering does not carry a
    `channel` in the stable contract, so we use the tenant default (same
    granularity as the Phase 1 allowlist). A disabled bundle grants nothing."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM dse_access_bundle
            WHERE tenant_id = %s
              AND channel IS NULL
              AND enabled = true
              AND designated_approvers @> %s::jsonb
            """,
            (tenant_id, json.dumps([principal_id])),
        )
        return cur.fetchone() is not None


def _in_steering_allowlist(conn, tenant_id: str, principal_id: str) -> bool:
    """Phase 1 fallback: membership in `tenant_steering_allowlist`
    (tenant-scoped). No row = not authorized through this path."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM tenant_steering_allowlist WHERE tenant_id = %s AND principal_id = %s",
            (tenant_id, principal_id),
        )
        return cur.fetchone() is not None
