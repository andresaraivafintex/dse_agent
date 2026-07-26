"""WSF-E3-T2 — access bundles: CRUD + enforcement + approver cascade.
Runs against real Postgres (migrations/0013_wsf2.sql)."""
from __future__ import annotations

import uuid

import pytest
from dse_audit.client import get_connection
from dse_identity import resolve_principal
from dse_platform import (
    AccessDenied,
    NoApproverError,
    check_mode_allowed,
    check_repo_allowed,
    get_effective_bundle,
    is_action_blocked,
    require_action_allowed,
    require_plan_approver,
    resolve_plan_approvers,
    upsert_access_bundle,
)
from dse_platform.sso import offboard, provision_console_user


@pytest.fixture()
def tenant():
    return f"acme-ab-{uuid.uuid4().hex[:8]}"


def _audit_actions(tenant_id):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT action FROM audit_log WHERE tenant_id = %s ORDER BY ts, id", (tenant_id,))
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def test_upsert_and_effective_resolution_channel_over_default(tenant):
    upsert_access_bundle(tenant, allowed_repos=["org/default-repo"], modes=["scope"])
    upsert_access_bundle(tenant, channel="C123", allowed_repos=["org/chan-repo"], modes=["implement_low_risk"])

    default = get_effective_bundle(tenant)
    assert default.allowed_repos == ["org/default-repo"]

    chan = get_effective_bundle(tenant, channel="C123")
    assert chan.allowed_repos == ["org/chan-repo"]

    # a channel with no specific bundle falls back to the tenant default
    other = get_effective_bundle(tenant, channel="C999")
    assert other.allowed_repos == ["org/default-repo"]


def test_no_bundle_denies_by_default(tenant):
    assert get_effective_bundle(tenant) is None
    assert check_repo_allowed(tenant, "org/x") is False
    assert check_mode_allowed(tenant, "scope") is False
    assert "access_denied_repo" in _audit_actions(tenant)


def test_repo_allowlist_enforcement(tenant):
    upsert_access_bundle(tenant, allowed_repos=["org/allowed"], modes=["scope"])
    assert check_repo_allowed(tenant, "org/allowed") is True
    assert check_repo_allowed(tenant, "org/forbidden") is False
    with pytest.raises(AccessDenied):
        require_repo_allowed_check(tenant)


def require_repo_allowed_check(tenant):
    from dse_platform import require_repo_allowed

    require_repo_allowed(tenant, "org/forbidden")


def test_mode_enforcement(tenant):
    upsert_access_bundle(tenant, modes=["scope", "implement_low_risk"])
    assert check_mode_allowed(tenant, "scope") is True
    assert check_mode_allowed(tenant, "security_review") is False
    with pytest.raises(ValueError):
        check_mode_allowed(tenant, "not_a_mode")


def test_blocked_action_and_require_action_allowed(tenant):
    upsert_access_bundle(tenant, blocked_actions=["direct_merge_to_protected_branch"], modes=["scope"])
    assert is_action_blocked(tenant, "direct_merge_to_protected_branch") is True
    assert is_action_blocked(tenant, "open_pr") is False
    with pytest.raises(AccessDenied):
        require_action_allowed(tenant, "direct_merge_to_protected_branch")
    # an action that is not blocked, with a bundle present: passes
    require_action_allowed(tenant, "open_pr")


def test_require_action_allowed_denies_when_no_bundle(tenant):
    with pytest.raises(AccessDenied):
        require_action_allowed(tenant, "direct_merge_to_protected_branch")


def test_invalid_mode_and_learning_scope_rejected(tenant):
    with pytest.raises(ValueError):
        upsert_access_bundle(tenant, modes=["bogus"])
    with pytest.raises(ValueError):
        upsert_access_bundle(tenant, learning_scope="everything")


# ---- approver cascade (P3: an empty cascade BLOCKS) ----
def test_codeowners_take_precedence_then_designated_fallback(tenant):
    owner = resolve_principal("github", f"owner-{uuid.uuid4().hex[:8]}")
    designated = resolve_principal("github", f"desig-{uuid.uuid4().hex[:8]}")
    upsert_access_bundle(tenant, designated_approvers=[designated], modes=["scope"])

    # with CODEOWNERS: use CODEOWNERS
    assert resolve_plan_approvers(tenant, codeowners=[owner]) == [owner]
    # without CODEOWNERS: fall back to the bundle's designated approvers
    assert resolve_plan_approvers(tenant) == [designated]


def test_empty_cascade_blocks(tenant):
    # no CODEOWNERS and no designated approvers => empty cascade => blocks
    upsert_access_bundle(tenant, modes=["scope"])
    assert resolve_plan_approvers(tenant) == []
    with pytest.raises(NoApproverError):
        require_plan_approver(tenant)
    assert "plan_gate_blocked_no_approver" in _audit_actions(tenant)


def test_offboarded_approver_removed_from_cascade(tenant):
    active = resolve_principal("github", f"act-{uuid.uuid4().hex[:8]}")
    gone = resolve_principal("github", f"gone-{uuid.uuid4().hex[:8]}")
    # provision 'gone' in the console and offboard it; 'active' is never provisioned (treated as active)
    provision_console_user(gone, f"sub-{gone}", roles=["approver"])
    offboard(gone, reason="left company", actor="system:test")

    upsert_access_bundle(tenant, designated_approvers=[gone, active], modes=["scope"])
    resolved = resolve_plan_approvers(tenant)
    assert gone not in resolved
    assert active in resolved
    assert "approvers_filtered_offboarded" in _audit_actions(tenant)
