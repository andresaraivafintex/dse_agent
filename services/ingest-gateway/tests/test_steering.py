"""Steering authorization.

Fase 1 (WSA-E6-T2a): fallback de allowlist, nunca 'qualquer um pode steerar'.
Fase 4 (WSA-E6-T2b): resolução identity-map-backed (papel via
`dse_console_identity` do RBAC do console + `dse_access_bundle.designated_approvers`
do WS-F), com a allowlist da Fase 1 como fallback documentado. A ASSINATURA de
`is_authorized_to_steer(tenant_id, principal_id) -> bool` não mudou — os testes
da Fase 1 abaixo continuam valendo sem alteração (contrato estável).

Testes reais contra o Postgres da infra (P8 — nunca mock de DB). O `conftest`
autouse limpa as linhas de teste (prefixos `test_tenant_` / `usr_test_`).
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
    """Cria um principal de teste (prefixo `usr_test_` p/ o cleanup do conftest)."""
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
    """Cria o access bundle default (channel NULL) do tenant com o principal
    como designated_approver."""
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
# Fase 1 — allowlist fallback (contrato estável; devem continuar passando)
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
# Fase 4 — identity-map-backed (papel via console RBAC + access bundle)
# ---------------------------------------------------------------------------
def test_console_role_authorizes_steering(tenant_id):
    """Usuário com papel resolvido (RBAC do console) steera — sem precisar de
    linha na allowlist."""
    p = _mk_principal()
    _add_console_identity(p, roles=["operator"], tenant_id=tenant_id)
    assert is_authorized_to_steer(tenant_id, p) is True


def test_bundle_designated_approver_authorizes_steering(tenant_id):
    """Usuário com papel resolvido via access bundle (designated_approvers)
    steera — a via de bundle do enunciado (WSA-E6-T2b)."""
    p = _mk_principal()
    _add_bundle_approver(tenant_id, p)
    assert is_authorized_to_steer(tenant_id, p) is True


def test_console_identity_without_steering_role_denied(tenant_id):
    """Ter identidade de console mas sem um papel em STEERING_ROLES (e sem
    allowlist/bundle) NÃO autoriza — deny-by-default."""
    p = _mk_principal()
    _add_console_identity(p, roles=["viewer"], tenant_id=tenant_id)
    assert is_authorized_to_steer(tenant_id, p) is False


def test_console_role_is_tenant_scoped(tenant_id):
    """Papel de console é escopado ao home tenant: um operador do tenant A não
    steera tarefas do tenant B só pelo papel (isolamento multi-tenant)."""
    other_tenant = tenant_id + "_other"
    p = _mk_principal()
    _add_console_identity(p, roles=["operator"], tenant_id=tenant_id)
    assert is_authorized_to_steer(tenant_id, p) is True
    assert is_authorized_to_steer(other_tenant, p) is False


def test_platform_operator_role_spans_tenants(tenant_id):
    """Operador de plataforma (home tenant NULL) com papel steera qualquer
    tenant."""
    other_tenant = tenant_id + "_other"
    p = _mk_principal()
    _add_console_identity(p, roles=["platform_admin"], tenant_id=None)
    assert is_authorized_to_steer(tenant_id, p) is True
    assert is_authorized_to_steer(other_tenant, p) is True


def test_offboarded_principal_denied_even_if_allowlisted(tenant_id):
    """Offboarding é a autoridade que sobrepõe (WSF-E3-T3): principal desativado
    no console NÃO steera mesmo continuando na allowlist da Fase 1."""
    p = _mk_principal()
    _allowlist(tenant_id, p)
    assert is_authorized_to_steer(tenant_id, p) is True  # antes do offboarding
    _add_console_identity(p, roles=["operator"], tenant_id=tenant_id, active=False)
    assert is_authorized_to_steer(tenant_id, p) is False


def test_expired_contractor_denied_even_with_role(tenant_id):
    """Contractor expirado (expires_at no passado) é negado apesar do papel."""
    p = _mk_principal()
    _add_console_identity(
        p, roles=["approver"], tenant_id=tenant_id, active=True,
        expires_at="2000-01-01T00:00:00Z",
    )
    assert is_authorized_to_steer(tenant_id, p) is False


def test_disabled_bundle_does_not_authorize(tenant_id):
    """Um access bundle desabilitado não concede papel (enabled=false)."""
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
# P8 — evidência: grant emite proveniência; rejeição (via correlate) audita
# ---------------------------------------------------------------------------
def test_grant_emits_provenance_audit(tenant_id):
    """Caminho positivo emite `steering_authorized` com o método de resolução
    (evidência de COMO a autorização foi concedida — P8)."""
    p = _mk_principal()
    _add_console_identity(p, roles=["operator"], tenant_id=tenant_id)
    assert _count_audit(tenant_id, "steering_authorized") == 0
    assert is_authorized_to_steer(tenant_id, p) is True
    assert _count_audit(tenant_id, "steering_authorized") == 1


def test_unauthorized_steering_does_not_signal_and_audits(tenant_id):
    """Invariante da Fase 1 preservada sob a nova implementação: um steering de
    principal SEM papel/allowlist/bundle sobre uma tarefa ativa retorna
    'unauthorized', NÃO vira signal, e gera audit `steering_rejected_unauthorized`
    (o audit de rejeição continua sendo emitido por correlate)."""
    stranger = _mk_principal("Stranger")
    conn = _conn()
    try:
        # cria uma tarefa ATIVA correlacionável pelo source_ref do evento
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
