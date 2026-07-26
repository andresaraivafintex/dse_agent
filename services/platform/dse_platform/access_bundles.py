"""WSF-E3-T2 — Per-tenant/channel access bundles (§10.18).

An *access bundle* is the administrable unit that states, for one
(tenant, channel): allowed repos, enabled modes, budgets, blocked actions,
designated approvers (the fallback of the CODEOWNERS cascade in WS-B's plan gate)
and learning scope (skill promotion). This module is both the administrable CRUD
and the *enforcement* consulted at the other workstreams' decision points.

Principles:
- **P1 (deterministic-or-human)**: every decision here is a set/string comparison
  in code — no LLM decides whether a repo/mode/action is allowed.
- **P3 / an empty cascade blocks**: `resolve_plan_approvers` returns the cascade
  (CODEOWNERS -> the bundle's designated_approvers). If the result is empty, the
  caller MUST block (WS-B's plan gate never self-approves) — this module exposes
  `require_plan_approver`, which raises `NoApproverError` in that case, so that
  "empty blocks" is structural rather than a forgettable check.
- **P8 (evidence over assertion)**: every negative enforcement decision (repo
  denied, action blocked, mode not allowed, approver missing) writes an audit row
  via `dse_audit.emit`.

Resolution (tenant, channel): the channel-specific bundle first; if it does not
exist (or is disabled), fall back to the tenant's default bundle (channel IS
NULL). If neither exists, `get_effective_bundle` returns None and enforcement
treats that as "nothing allowed" (deny-by-default, never "no limit").
"""
from __future__ import annotations

import dataclasses
import os
from typing import Any

import psycopg2
import psycopg2.extras
from dse_audit import emit

_DSN = os.environ.get(
    "DSE_PLATFORM_DATABASE_URL",
    os.environ.get("DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"),
)

VALID_MODES = {"scope", "ask", "implement_low_risk", "security_review"}


def _get_connection():
    return psycopg2.connect(_DSN)


class AccessDenied(Exception):
    """Enforcement denied an operation (repo/mode/action). Fail-closed at the
    boundary (P6: never proceeds halfway)."""


class NoApproverError(Exception):
    """The plan approver cascade resolved empty — the gate MUST block (P3: never
    self-approve). Raised by `require_plan_approver`."""


@dataclasses.dataclass(frozen=True)
class AccessBundle:
    id: int
    tenant_id: str
    channel: str | None
    allowed_repos: list[str]
    modes: list[str]
    budgets: dict[str, Any]
    blocked_actions: list[str]
    designated_approvers: list[str]
    learning_scope: str
    enabled: bool


def _row_to_bundle(row) -> AccessBundle:
    return AccessBundle(
        id=row["id"],
        tenant_id=row["tenant_id"],
        channel=row["channel"],
        allowed_repos=list(row["allowed_repos"] or []),
        modes=list(row["modes"] or []),
        budgets=dict(row["budgets"] or {}),
        blocked_actions=list(row["blocked_actions"] or []),
        designated_approvers=list(row["designated_approvers"] or []),
        learning_scope=row["learning_scope"],
        enabled=row["enabled"],
    )


# ---------------------------------------------------------------------------
# Administrable CRUD
# ---------------------------------------------------------------------------
def upsert_access_bundle(
    tenant_id: str,
    *,
    channel: str | None = None,
    allowed_repos: list[str] | None = None,
    modes: list[str] | None = None,
    budgets: dict[str, Any] | None = None,
    blocked_actions: list[str] | None = None,
    designated_approvers: list[str] | None = None,
    learning_scope: str | None = None,
    enabled: bool | None = None,
    actor: str = "system:platform-admin",
    conn=None,
) -> AccessBundle:
    """Creates/updates the bundle of (tenant, channel). None fields are left
    untouched on an update (partial idempotency). Writes audit (P8)."""
    if modes is not None:
        invalid = set(modes) - VALID_MODES
        if invalid:
            raise ValueError(f"invalid modes: {sorted(invalid)} (valid: {sorted(VALID_MODES)})")
    if learning_scope is not None and learning_scope not in {"none", "tenant", "global"}:
        raise ValueError(f"invalid learning_scope: {learning_scope!r}")

    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            # a NULL channel uses its own partial unique index; ON CONFLICT needs
            # the right index depending on whether channel is NULL or not.
            if channel is None:
                conflict = "(tenant_id) WHERE channel IS NULL"
            else:
                conflict = "(tenant_id, channel) WHERE channel IS NOT NULL"
            cur.execute(
                f"""
                INSERT INTO dse_access_bundle
                    (tenant_id, channel, allowed_repos, modes, budgets,
                     blocked_actions, designated_approvers, learning_scope, enabled)
                VALUES (%s, %s,
                        COALESCE(%s::jsonb, '[]'::jsonb),
                        COALESCE(%s::jsonb, '[]'::jsonb),
                        COALESCE(%s::jsonb, '{{}}'::jsonb),
                        COALESCE(%s::jsonb, '[]'::jsonb),
                        COALESCE(%s::jsonb, '[]'::jsonb),
                        COALESCE(%s, 'none'),
                        COALESCE(%s, true))
                ON CONFLICT {conflict} DO UPDATE SET
                    allowed_repos = COALESCE(EXCLUDED.allowed_repos, dse_access_bundle.allowed_repos),
                    modes = COALESCE(EXCLUDED.modes, dse_access_bundle.modes),
                    budgets = COALESCE(EXCLUDED.budgets, dse_access_bundle.budgets),
                    blocked_actions = COALESCE(EXCLUDED.blocked_actions, dse_access_bundle.blocked_actions),
                    designated_approvers = COALESCE(EXCLUDED.designated_approvers, dse_access_bundle.designated_approvers),
                    learning_scope = COALESCE(EXCLUDED.learning_scope, dse_access_bundle.learning_scope),
                    enabled = COALESCE(EXCLUDED.enabled, dse_access_bundle.enabled)
                """,
                (
                    tenant_id,
                    channel,
                    _json_or_none(allowed_repos),
                    _json_or_none(modes),
                    _json_or_none(budgets),
                    _json_or_none(blocked_actions),
                    _json_or_none(designated_approvers),
                    learning_scope,
                    enabled,
                ),
            )
        emit(
            actor=actor,
            action="access_bundle_upserted",
            tenant_id=tenant_id,
            details={"channel": channel},
            conn=conn,
        )
        if owns:
            conn.commit()
    except Exception:
        if owns:
            conn.rollback()
        raise
    finally:
        if owns:
            conn.close()

    result = get_bundle(tenant_id, channel=channel)
    assert result is not None
    return result


def _json_or_none(v):
    return None if v is None else psycopg2.extras.Json(v)


def get_bundle(tenant_id: str, *, channel: str | None = None, conn=None) -> AccessBundle | None:
    """Fetches the EXACT bundle of (tenant, channel) — no fallback. Use
    `get_effective_bundle` for the resolution with fallback."""
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            if channel is None:
                cur.execute(
                    "SELECT * FROM dse_access_bundle WHERE tenant_id = %s AND channel IS NULL",
                    (tenant_id,),
                )
            else:
                cur.execute(
                    "SELECT * FROM dse_access_bundle WHERE tenant_id = %s AND channel = %s",
                    (tenant_id, channel),
                )
            row = cur.fetchone()
    finally:
        if owns:
            conn.close()
    return _row_to_bundle(row) if row else None


def get_effective_bundle(tenant_id: str, *, channel: str | None = None, conn=None) -> AccessBundle | None:
    """Effective bundle for (tenant, channel): the channel-specific one (if it
    exists and is enabled), otherwise the tenant default (channel IS NULL, if
    enabled). None if neither — enforcement treats None as deny-by-default."""
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        specific = None
        if channel is not None:
            specific = get_bundle(tenant_id, channel=channel, conn=conn)
            if specific is not None and specific.enabled:
                return specific
        default = get_bundle(tenant_id, channel=None, conn=conn)
        if default is not None and default.enabled:
            return default
        # the channel-specific one exists but is disabled, and there is no default: no bundle.
        return None
    finally:
        if owns:
            conn.close()


def list_bundles(tenant_id: str, conn=None) -> list[AccessBundle]:
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT * FROM dse_access_bundle WHERE tenant_id = %s ORDER BY channel NULLS FIRST",
                (tenant_id,),
            )
            rows = cur.fetchall()
    finally:
        if owns:
            conn.close()
    return [_row_to_bundle(r) for r in rows]


# ---------------------------------------------------------------------------
# Enforcement (consulted at the decision points)
# ---------------------------------------------------------------------------
def _audit_denial(tenant_id: str, work_item_id: str | None, action: str, details: dict, conn=None) -> None:
    emit(
        actor="system:platform-enforcement",
        action=action,
        tenant_id=tenant_id,
        work_item_id=work_item_id,
        details=details,
        conn=conn,
    )


def check_repo_allowed(
    tenant_id: str, repo: str, *, channel: str | None = None, work_item_id: str | None = None
) -> bool:
    """True iff `repo` is in the effective bundle's allowlist. No bundle, or a
    repo outside the list = denied (deny-by-default) + audit."""
    bundle = get_effective_bundle(tenant_id, channel=channel)
    if bundle is not None and repo in bundle.allowed_repos:
        return True
    _audit_denial(
        tenant_id, work_item_id, "access_denied_repo",
        {"repo": repo, "channel": channel, "reason": "no_bundle" if bundle is None else "repo_not_in_allowlist"},
    )
    return False


def require_repo_allowed(tenant_id: str, repo: str, *, channel: str | None = None, work_item_id: str | None = None) -> None:
    if not check_repo_allowed(tenant_id, repo, channel=channel, work_item_id=work_item_id):
        raise AccessDenied(f"repo {repo!r} not allowed for tenant {tenant_id!r} (channel={channel!r})")


def check_mode_allowed(
    tenant_id: str, mode: str, *, channel: str | None = None, work_item_id: str | None = None
) -> bool:
    """True iff `mode` is enabled in the effective bundle. deny-by-default + audit."""
    if mode not in VALID_MODES:
        raise ValueError(f"unknown mode: {mode!r}")
    bundle = get_effective_bundle(tenant_id, channel=channel)
    if bundle is not None and mode in bundle.modes:
        return True
    _audit_denial(
        tenant_id, work_item_id, "access_denied_mode",
        {"mode": mode, "channel": channel, "reason": "no_bundle" if bundle is None else "mode_not_enabled"},
    )
    return False


def is_action_blocked(
    tenant_id: str, action: str, *, channel: str | None = None, work_item_id: str | None = None
) -> bool:
    """True iff `action` is in the effective bundle's blocked_actions (e.g.
    "direct_merge_to_protected_branch"). With no bundle => nothing is explicitly
    blocked by list, BUT the caller of a sensitive action must use
    `require_action_allowed`, which also denies when there is no bundle."""
    bundle = get_effective_bundle(tenant_id, channel=channel)
    if bundle is None:
        return False
    blocked = action in bundle.blocked_actions
    if blocked:
        _audit_denial(
            tenant_id, work_item_id, "access_denied_action",
            {"action": action, "channel": channel, "reason": "action_in_blocklist"},
        )
    return blocked


def require_action_allowed(
    tenant_id: str, action: str, *, channel: str | None = None, work_item_id: str | None = None
) -> None:
    """Raises AccessDenied if the action is blocked OR if there is no bundle
    (deny-by-default for sensitive actions)."""
    bundle = get_effective_bundle(tenant_id, channel=channel)
    if bundle is None:
        _audit_denial(
            tenant_id, work_item_id, "access_denied_action",
            {"action": action, "channel": channel, "reason": "no_bundle"},
        )
        raise AccessDenied(f"action {action!r} denied: no access bundle for tenant {tenant_id!r}")
    if action in bundle.blocked_actions:
        _audit_denial(
            tenant_id, work_item_id, "access_denied_action",
            {"action": action, "channel": channel, "reason": "action_in_blocklist"},
        )
        raise AccessDenied(f"action {action!r} blocked by the bundle of {tenant_id!r} (channel={channel!r})")


# ---------------------------------------------------------------------------
# Plan gate approver cascade (WSB-E3-T2) — CODEOWNERS fallback
# ---------------------------------------------------------------------------
def resolve_plan_approvers(
    tenant_id: str,
    *,
    channel: str | None = None,
    codeowners: list[str] | None = None,
    work_item_id: str | None = None,
    conn=None,
) -> list[str]:
    """Plan gate cascade: resolved CODEOWNERS (passed by WS-B from the repo)
    FIRST; if empty/absent, fall back to the effective bundle's
    `designated_approvers`. Offboarded approvers (dse_console_identity.active =
    false) are REMOVED from the cascade (WSF-E3-T3).

    Returns the final list (may be empty — the caller decides;
    `require_plan_approver` makes "empty blocks" structural)."""
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        candidates: list[str] = []
        for p in (codeowners or []):
            if p and p not in candidates:
                candidates.append(p)
        if not candidates:
            bundle = get_effective_bundle(tenant_id, channel=channel, conn=conn)
            if bundle is not None:
                for p in bundle.designated_approvers:
                    if p and p not in candidates:
                        candidates.append(p)

        # drop offboarded/expired principals from the resolution (WSF-E3-T3 offboarding)
        active = _filter_active_principals(candidates, conn=conn)
        removed = [p for p in candidates if p not in active]
        if removed:
            emit(
                actor="system:platform-enforcement",
                action="approvers_filtered_offboarded",
                tenant_id=tenant_id,
                work_item_id=work_item_id,
                details={"removed": removed, "channel": channel},
                conn=conn,
            )
            if owns:
                conn.commit()
        return active
    finally:
        if owns:
            conn.close()


def _filter_active_principals(principals: list[str], conn=None) -> list[str]:
    """Keeps only the principals that are NOT deactivated in the console
    identity. A principal with no row in dse_console_identity is considered
    active (it may be a CODEOWNER who never logged into the console — we do not
    drop it for that; we only drop the EXPLICITLY deactivated/expired ones)."""
    if not principals:
        return []
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT principal_id FROM dse_console_identity
                WHERE principal_id = ANY(%s)
                  AND (active = false OR (expires_at IS NOT NULL AND expires_at < now()))
                """,
                (principals,),
            )
            inactive = {r[0] for r in cur.fetchall()}
    finally:
        if owns:
            conn.close()
    return [p for p in principals if p not in inactive]


def require_plan_approver(
    tenant_id: str,
    *,
    channel: str | None = None,
    codeowners: list[str] | None = None,
    work_item_id: str | None = None,
) -> list[str]:
    """Same as `resolve_plan_approvers`, but raises `NoApproverError` if the
    cascade resolves empty — the spec's "an empty cascade BLOCKS", made
    structural (P1/P3: the gate never self-approves for lack of an approver)."""
    approvers = resolve_plan_approvers(
        tenant_id, channel=channel, codeowners=codeowners, work_item_id=work_item_id
    )
    if not approvers:
        _audit_denial(
            tenant_id, work_item_id, "plan_gate_blocked_no_approver",
            {"channel": channel, "reason": "empty_cascade"},
        )
        raise NoApproverError(
            f"empty approver cascade for tenant {tenant_id!r} (channel={channel!r}): gate blocked"
        )
    return approvers
