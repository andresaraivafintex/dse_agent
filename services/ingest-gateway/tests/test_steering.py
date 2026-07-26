"""Steering authorization.

Phase 1 (WSA-E6-T2a): allowlist fallback, never 'anyone can steer'.
Phase 4 (WSA-E6-T2b): identity-map-backed resolution (role via
`dse_console_identity` from the console RBAC + WS-F's
`dse_access_bundle.designated_approvers`), with the Phase 1 allowlist as the
documented fallback. The SIGNATURE of
`is_authorized_to_steer(tenant_id, principal_id) -> bool` did not change — the
Phase 1 tests below still hold unchanged (stable contract).

Real tests against the infra Postgres (P8 — never mock the DB). The autouse
`conftest` cleans up the test rows (prefixes `test_tenant_` / `usr_test_`).
"""
from __future__ import annotations

import json
import uuid

import psycopg2

from dse_contracts import Actor, ConversationEvent, EventKind, Platform
from ingest_gateway.correlate import correlate
from ingest_gateway.steering import is_authorized_to_steer

DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _conn():
    return psycopg2.connect(DSN)


def _mk_principal(display_name: str = "Test User") -> str:
    """Creates a test principal (prefix `usr_test_` for the conftest cleanup)."""
    principal_id = f"usr_test_{uuid.uuid4().hex[:12]}"
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO principals (id, display_name) VALUES (%s, %s)",
            (principal_id, display_name),
        )
    conn.commit()
    conn.close()
    return principal_id


def _add_console_identity(
    principal_id: str,
    *,
    roles: list[str],
    tenant_id: str | None,
    active: bool = True,
    expires_at: str | None = None,
) -> None:
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO dse_console_identity
                (principal_id, sso_subject, tenant_id, roles, active, expires_at)
            VALUES (%s, %s, %s, %s::jsonb, %s, %s)
            """,
            (
                principal_id,
                f"sub_{uuid.uuid4().hex[:12]}",
                tenant_id,
                json.dumps(roles),
                active,
                expires_at,
            ),
        )
    conn.commit()
    conn.close()


def _add_bundle_approver(tenant_id: str, principal_id: str) -> None:
    """Creates the tenant's default access bundle (channel NULL) with the
    principal as a designated_approver."""
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO dse_access_bundle (tenant_id, channel, designated_approvers, enabled)
            VALUES (%s, NULL, %s::jsonb, true)
            ON CONFLICT (tenant_id) WHERE channel IS NULL
            DO UPDATE SET designated_approvers = EXCLUDED.designated_approvers, enabled = true
            """,
            (tenant_id, json.dumps([principal_id])),
        )
    conn.commit()
    conn.close()


def _allowlist(tenant_id: str, principal_id: str) -> None:
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenant_steering_allowlist (tenant_id, principal_id) VALUES (%s, %s)",
            (tenant_id, principal_id),
        )
    conn.commit()
    conn.close()


def _count_audit(tenant_id: str, action: str) -> int:
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM audit_log WHERE tenant_id = %s AND action = %s",
                (tenant_id, action),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()


def _steering_event(stranger_principal: str) -> ConversationEvent:
    return ConversationEvent.build(
        platform=Platform.slack,
        thread_key="C1:1700000000.0001",
        message_id="1700000000.0002",
        kind=EventKind.steering,
        source_ref={"channel": "C1", "thread_ts": "1700000000.0001"},
        actor=Actor(platform_user_id="U_stranger", resolved_principal=stranger_principal),
        content_snapshot="please also handle the edge case",
        signature_verified=True,
    )


# ---------------------------------------------------------------------------
# Phase 1 — allowlist fallback (stable contract; these must keep passing)
# ---------------------------------------------------------------------------
def test_absent_row_means_unauthorized(tenant_id):
    assert is_authorized_to_steer(tenant_id, "usr_never_added") is False


def test_allowlisted_principal_is_authorized(tenant_id):
    _allowlist(tenant_id, "usr_ops_lead")
    assert is_authorized_to_steer(tenant_id, "usr_ops_lead") is True


def test_allowlist_is_scoped_per_tenant(tenant_id):
    other_tenant = tenant_id + "_other"
    _allowlist(tenant_id, "usr_scoped")

    assert is_authorized_to_steer(tenant_id, "usr_scoped") is True
    assert is_authorized_to_steer(other_tenant, "usr_scoped") is False


# ---------------------------------------------------------------------------
# Phase 4 — identity-map-backed (role via console RBAC + access bundle)
# ---------------------------------------------------------------------------
def test_console_role_authorizes_steering(tenant_id):
    """A user with a resolved role (console RBAC) can steer — no allowlist row
    needed."""
    p = _mk_principal()
    _add_console_identity(p, roles=["operator"], tenant_id=tenant_id)
    assert is_authorized_to_steer(tenant_id, p) is True


def test_bundle_designated_approver_authorizes_steering(tenant_id):
    """A user with a role resolved via the access bundle (designated_approvers)
    can steer — the bundle path from the spec (WSA-E6-T2b)."""
    p = _mk_principal()
    _add_bundle_approver(tenant_id, p)
    assert is_authorized_to_steer(tenant_id, p) is True


def test_console_identity_without_steering_role_denied(tenant_id):
    """Having a console identity but no role in STEERING_ROLES (and no
    allowlist/bundle) does NOT authorize — deny-by-default."""
    p = _mk_principal()
    _add_console_identity(p, roles=["viewer"], tenant_id=tenant_id)
    assert is_authorized_to_steer(tenant_id, p) is False


def test_console_role_is_tenant_scoped(tenant_id):
    """A console role is scoped to the home tenant: an operator of tenant A does
    not steer tenant B's tasks by role alone (multi-tenant isolation)."""
    other_tenant = tenant_id + "_other"
    p = _mk_principal()
    _add_console_identity(p, roles=["operator"], tenant_id=tenant_id)
    assert is_authorized_to_steer(tenant_id, p) is True
    assert is_authorized_to_steer(other_tenant, p) is False


def test_platform_operator_role_spans_tenants(tenant_id):
    """A platform operator (NULL home tenant) with a role steers any tenant."""
    other_tenant = tenant_id + "_other"
    p = _mk_principal()
    _add_console_identity(p, roles=["platform_admin"], tenant_id=None)
    assert is_authorized_to_steer(tenant_id, p) is True
    assert is_authorized_to_steer(other_tenant, p) is True


def test_offboarded_principal_denied_even_if_allowlisted(tenant_id):
    """Offboarding is the authority that overrides (WSF-E3-T3): a principal
    deactivated in the console does NOT steer even while still on the Phase 1
    allowlist."""
    p = _mk_principal()
    _allowlist(tenant_id, p)
    assert is_authorized_to_steer(tenant_id, p) is True  # before offboarding
    _add_console_identity(p, roles=["operator"], tenant_id=tenant_id, active=False)
    assert is_authorized_to_steer(tenant_id, p) is False


def test_expired_contractor_denied_even_with_role(tenant_id):
    """An expired contractor (expires_at in the past) is denied despite the role."""
    p = _mk_principal()
    _add_console_identity(
        p, roles=["approver"], tenant_id=tenant_id, active=True,
        expires_at="2000-01-01T00:00:00Z",
    )
    assert is_authorized_to_steer(tenant_id, p) is False


def test_disabled_bundle_does_not_authorize(tenant_id):
    """A disabled access bundle grants no role (enabled=false)."""
    p = _mk_principal()
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO dse_access_bundle (tenant_id, channel, designated_approvers, enabled)
            VALUES (%s, NULL, %s::jsonb, false)
            """,
            (tenant_id, json.dumps([p])),
        )
    conn.commit()
    conn.close()
    assert is_authorized_to_steer(tenant_id, p) is False


# ---------------------------------------------------------------------------
# P8 — evidence: the grant emits provenance; the rejection (via correlate) audits
# ---------------------------------------------------------------------------
def test_grant_emits_provenance_audit(tenant_id):
    """The positive path emits `steering_authorized` with the resolution method
    (evidence of HOW the authorization was granted — P8)."""
    p = _mk_principal()
    _add_console_identity(p, roles=["operator"], tenant_id=tenant_id)
    assert _count_audit(tenant_id, "steering_authorized") == 0
    assert is_authorized_to_steer(tenant_id, p) is True
    assert _count_audit(tenant_id, "steering_authorized") == 1


def test_unauthorized_steering_does_not_signal_and_audits(tenant_id):
    """Phase 1 invariant preserved under the new implementation: steering by a
    principal WITHOUT role/allowlist/bundle on an active task returns
    'unauthorized', does NOT become a signal, and emits the
    `steering_rejected_unauthorized` audit row (the rejection audit is still
    emitted by correlate)."""
    stranger = _mk_principal("Stranger")
    conn = _conn()
    try:
        # creates an ACTIVE task correlatable by the event's source_ref
        event = _steering_event(stranger)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO work_items
                    (id, tenant_id, source, source_ref, status, requester, idempotency_key)
                VALUES (%s, %s, 'slack', %s::jsonb, 'running', %s, %s)
                """,
                (
                    f"wi_{uuid.uuid4().hex[:12]}",
                    tenant_id,
                    json.dumps(event.source_ref),
                    "usr_test_requester",
                    f"idem_{uuid.uuid4().hex[:12]}",
                ),
            )
        conn.commit()

        result = correlate(
            conn, tenant_id=tenant_id, event=event, requester_principal=stranger
        )
        conn.commit()
        assert result.kind == "unauthorized"
    finally:
        conn.close()

    assert _count_audit(tenant_id, "steering_rejected_unauthorized") == 1
