"""WSF-E6-T3 — UI do queue board (FastAPI). Exercita SSO gate, projeção §9.3 na
página, e um controle end-to-end (form POST -> OperatorConsole -> FakeSignalSender
+ audit). Usa TestClient (httpx) — sem servidor externo."""
from __future__ import annotations

import uuid

import psycopg2.extras
import pytest
from dse_audit.client import get_connection
from dse_platform import upsert_tenant_config
from dse_platform.dev_idp import DevIdP
from dse_platform.queue_board.app import create_app
from dse_platform.queue_board.signals import FakeSignalSender
from dse_platform.sso import OIDCVerifier
from fastapi.testclient import TestClient

AUDIENCE = "dse-admin-console"
ISSUER = "https://dev-idp.local"


@pytest.fixture()
def idp():
    return DevIdP(issuer=ISSUER)


@pytest.fixture()
def sender():
    return FakeSignalSender()


@pytest.fixture()
def client(idp, sender):
    verifier = OIDCVerifier(jwks=idp.jwks(), issuer=ISSUER, audience=AUDIENCE)
    app = create_app(sender=sender, verifier=verifier)
    return TestClient(app, follow_redirects=False)


def _insert_work_item(tenant, status="implementing"):
    wid = f"wi-{uuid.uuid4().hex[:10]}"
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO work_items (id, tenant_id, source, source_ref, requester, status, idempotency_key, repo) "
                "VALUES (%s,%s,'github',%s,%s,%s,%s,'org/repo')",
                (wid, tenant, psycopg2.extras.Json({"repo": "org/repo"}), "usr_req", status, wid),
            )
        conn.commit()
    finally:
        conn.close()
    return wid


def _login(client, idp):
    sub = f"sub-{uuid.uuid4().hex[:8]}"
    tok = idp.mint_id_token(subject=sub, audience=AUDIENCE, email="op@co", name="Op")
    r = client.post("/login", data={"id_token": tok})
    assert r.status_code == 302
    return sub


def test_board_requires_auth(client):
    r = client.get("/board")
    assert r.status_code == 401


def test_login_then_board_lists_tenants(client, idp):
    tenant = f"acme-ui-{uuid.uuid4().hex[:8]}"
    upsert_tenant_config(tenant)
    _insert_work_item(tenant)
    _login(client, idp)
    r = client.get("/board")
    assert r.status_code == 200
    assert tenant in r.text


def test_tenant_page_shows_all_states(client, idp):
    tenant = f"acme-ui-{uuid.uuid4().hex[:8]}"
    upsert_tenant_config(tenant)
    _insert_work_item(tenant, "implementing")
    _login(client, idp)
    r = client.get(f"/board/tenant/{tenant}")
    assert r.status_code == 200
    # todos os estados §9.3 renderizados como seções
    for state in ["new", "needs_clarification", "implementing", "pr_ready", "done", "escalated"]:
        assert state in r.text


def test_control_pause_via_form_sends_signal_and_audits(client, idp, sender):
    tenant = f"acme-ui-{uuid.uuid4().hex[:8]}"
    upsert_tenant_config(tenant)
    w = _insert_work_item(tenant)
    _login(client, idp)
    r = client.post("/control/pause", data={"work_item_id": w, "tenant_id": tenant, "reason": "ui-test"})
    assert r.status_code == 302
    assert any(sig == "pause" and wid == w for (wid, sig, _) in sender.sent)

    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT actor, action FROM audit_log WHERE work_item_id = %s", (w,))
            rows = cur.fetchall()
    finally:
        conn.close()
    assert any(r["action"] == "operator_pause" and r["actor"].startswith("usr_") for r in rows)


def test_offboarded_operator_denied_mid_session(client, idp):
    from dse_platform.sso import offboard

    sub = f"sub-{uuid.uuid4().hex[:8]}"
    tok = idp.mint_id_token(subject=sub, audience=AUDIENCE, name="Temp")
    client.post("/login", data={"id_token": tok})
    # descobre o principal e offboarda
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT principal_id FROM dse_console_identity WHERE sso_subject = %s", (sub,))
            principal = cur.fetchone()[0]
    finally:
        conn.close()
    offboard(principal, reason="revoked", actor="system:test")
    # a sessão (cookie) ainda existe, mas o re-check por request bloqueia
    r = client.get("/board")
    assert r.status_code == 403


def test_login_rejects_bad_token(client):
    r = client.post("/login", data={"id_token": "not.a.jwt"})
    assert r.status_code == 401


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True}
