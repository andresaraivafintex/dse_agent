"""Shared-session auth for the two consoles: the open-redirect defense on the
post-login landing spot, and the forward-auth endpoint the admin panel is
guarded by.

The panel is a Next.js app with no login of its own. Traefik asks
`/auth/verify` here before forwarding anything to it, so a mistake in these two
places is the difference between one guarded surface and one open one."""
from __future__ import annotations

import uuid

import pytest
from dse_platform.dev_idp import DevIdP
from dse_platform.queue_board.app import _safe_next, create_app
from dse_platform.queue_board.signals import FakeSignalSender
from dse_platform.sso import OIDCVerifier
from fastapi.testclient import TestClient

AUDIENCE = "dse-admin-console"
ISSUER = "https://dev-idp.local"
BOARD = "admin.example.com"
PANEL = "https://panel.example.com"


@pytest.fixture()
def idp():
    return DevIdP(issuer=ISSUER)


@pytest.fixture()
def client(idp):
    verifier = OIDCVerifier(jwks=idp.jwks(), issuer=ISSUER, audience=AUDIENCE)
    return TestClient(
        create_app(sender=FakeSignalSender(), verifier=verifier), follow_redirects=False
    )


def _login(client, idp):
    tok = idp.mint_id_token(
        subject=f"sub-{uuid.uuid4().hex[:8]}", audience=AUDIENCE, email="op@co", name="Op"
    )
    assert client.post("/login", data={"id_token": tok}).status_code == 302


class _Req:
    """Just the two attributes _safe_next reads."""

    def __init__(self, host=BOARD):
        self.headers = {"host": host}


@pytest.mark.parametrize(
    "raw",
    [
        "https://evil.example.com/steal",
        "//evil.example.com/steal",  # scheme-relative: a netloc without a scheme
        "http://admin.example.com.evil.com/",  # our host as a prefix of theirs
        "javascript:alert(1)",
        "https://evil.example.com\\@admin.example.com/",
    ],
)
def test_next_refuses_anywhere_we_do_not_control(monkeypatch, raw):
    monkeypatch.setenv("DSE_CONSOLE_URL", PANEL)
    assert _safe_next(_Req(), raw) == "/board"


def test_next_allows_relative_paths_and_our_own_hosts(monkeypatch):
    monkeypatch.setenv("DSE_CONSOLE_URL", PANEL)
    assert _safe_next(_Req(), "/board/tenant_x") == "/board/tenant_x"
    assert _safe_next(_Req(), f"{PANEL}/work-items") == f"{PANEL}/work-items"
    assert _safe_next(_Req(), f"https://{BOARD}/board") == f"https://{BOARD}/board"
    assert _safe_next(_Req(), None) == "/board"


def test_next_ignores_the_panel_when_no_panel_is_configured(monkeypatch):
    """An unset DSE_CONSOLE_URL must not become a wildcard."""
    monkeypatch.delenv("DSE_CONSOLE_URL", raising=False)
    assert _safe_next(_Req(), f"{PANEL}/work-items") == "/board"


def test_verify_sends_an_anonymous_visitor_to_login_and_back(client, monkeypatch):
    monkeypatch.setenv("DSE_QUEUE_BOARD_URL", f"https://{BOARD}")
    r = client.get(
        "/auth/verify",
        headers={
            "x-forwarded-proto": "https",
            "x-forwarded-host": "panel.example.com",
            "x-forwarded-uri": "/work-items?state=blocked",
        },
    )
    # Not a 401: forward-auth hands this response straight to the browser, so a
    # redirect is what turns "denied" into "here is where you sign in".
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith(f"https://{BOARD}/auth/login?")
    assert "panel.example.com" in loc and "state%3Dblocked" in loc


def test_verify_passes_a_signed_in_operator_and_names_them(client, idp):
    _login(client, idp)
    r = client.get("/auth/verify", headers={"x-forwarded-host": "panel.example.com"})
    assert r.status_code == 200
    # The panel reads identity from these rather than reparsing the cookie.
    assert r.headers["x-auth-principal"]
    assert r.headers["x-auth-subject"]


def test_verify_does_not_leak_the_panel_login_loop_without_config(client):
    """With no self URL configured the redirect is relative, not to the panel —
    a login link pointing at the guarded host would bounce forever."""
    r = client.get("/auth/verify", headers={"x-forwarded-host": "panel.example.com"})
    assert r.status_code == 302
    assert not r.headers["location"].startswith("https://panel.example.com")


def test_session_cookie_is_shared_across_hosts_when_a_domain_is_set(idp, monkeypatch):
    monkeypatch.setenv("DSE_CONSOLE_COOKIE_DOMAIN", ".example.com")
    verifier = OIDCVerifier(jwks=idp.jwks(), issuer=ISSUER, audience=AUDIENCE)
    c = TestClient(
        create_app(sender=FakeSignalSender(), verifier=verifier), follow_redirects=False
    )
    tok = idp.mint_id_token(subject="sub-dom", audience=AUDIENCE, email="op@co", name="Op")
    r = c.post("/login", data={"id_token": tok})
    assert "domain=.example.com" in r.headers["set-cookie"].lower()


def test_session_cookie_is_host_only_by_default(client, idp, monkeypatch):
    monkeypatch.delenv("DSE_CONSOLE_COOKIE_DOMAIN", raising=False)
    tok = idp.mint_id_token(subject="sub-nodom", audience=AUDIENCE, email="op@co", name="Op")
    r = client.post("/login", data={"id_token": tok})
    assert "domain=" not in r.headers["set-cookie"].lower()


def test_session_cookie_is_secure_only_behind_tls(client, idp):
    """Marking it Secure over plain http would make it undeliverable; not
    marking it behind TLS would let it travel over a downgraded request."""
    tok = idp.mint_id_token(subject="sub-tls", audience=AUDIENCE, email="op@co", name="Op")
    plain = client.post("/login", data={"id_token": tok})
    assert "secure" not in plain.headers["set-cookie"].lower()

    tok2 = idp.mint_id_token(subject="sub-tls2", audience=AUDIENCE, email="op@co", name="Op")
    tls = client.post(
        "/login", data={"id_token": tok2}, headers={"x-forwarded-proto": "https"}
    )
    assert "secure" in tls.headers["set-cookie"].lower()
