"""WSF-E6-T3 — minimal utility UI for the queue board (server-rendered, NO design
system) on port 8890. Shows every state of the §9.3 machine per tenant and fires
the operator controls. Access via SSO (WSF-E3-T3).

Deliberately ugly and framework-free: HTML assembled in Python, raw tables, POST
forms. The goal is operation, not aesthetics. Each control is a form that calls
an endpoint delegating to `OperatorConsole` (audit + signal).

Authentication: signed session cookie (HS256) issued after a successful SSO login
(`dse_platform.sso.login`). In dev, login accepts a pasted `id_token` (minted by
`DevIdP` — see README/gaps); in production, the login handler is swapped for a
real OIDC redirect to the customer's IdP (the rest — verify, session, offboarding
— is unchanged). Every request re-checks whether the account is still active
(offboarding takes effect immediately).
"""
from __future__ import annotations

import json
import os
import secrets
import time
import urllib.parse
import urllib.request

import jwt
from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from ..sso import ConsoleSession, is_console_active
from . import api
from .operator import OperatorConsole
from .signals import SignalSender, TemporalSignalSender

_SESSION_SECRET = os.environ.get(
    "DSE_CONSOLE_SESSION_SECRET", "dev-console-secret-not-for-prod-change-me-32b"
)
_SESSION_COOKIE = "dse_console_session"
_SESSION_TTL = 8 * 3600

# Short-lived cookie carrying the `state` and `nonce` of an in-flight login.
# Signed with the same session secret, so it cannot be forged by the browser.
_OIDC_TX_COOKIE = "dse_console_oidc_tx"
_OIDC_TX_TTL = 600


def _oidc_settings() -> dict | None:
    """Everything the authorization-code flow needs, or None if not configured.

    Kept separate from the verifier: the verifier only has to CHECK a token,
    while this drives the redirect dance that obtains one. A deployment can
    legitimately have the first without the second (a token minted elsewhere and
    posted in), which is how this service worked before.
    """
    issuer = os.environ.get("DSE_OIDC_ISSUER")
    client_id = os.environ.get("DSE_OIDC_CLIENT_ID") or os.environ.get("DSE_OIDC_AUDIENCE")
    client_secret = os.environ.get("DSE_OIDC_CLIENT_SECRET")
    if not (issuer and client_id and client_secret):
        return None
    return {"issuer": issuer.rstrip("/"), "client_id": client_id, "client_secret": client_secret}


def _oidc_endpoints(issuer: str) -> dict | None:
    """Authorization and token endpoints, read from the provider's discovery
    document rather than assembled by hand — the URL shape differs between
    providers and even between versions of the same one."""
    try:
        with urllib.request.urlopen(issuer + "/.well-known/openid-configuration", timeout=10) as r:
            doc = json.load(r)
        return {"authorization_endpoint": doc["authorization_endpoint"],
                "token_endpoint": doc["token_endpoint"]}
    except Exception:  # noqa: BLE001 — surfaced to the caller as "login unavailable"
        return None


def _self_base_url() -> str:
    """This service's own public origin, e.g. `https://admin.example.com`.

    Only needed when answering a forward-auth probe, where every forwarded
    header describes the app being protected instead of this one and there is
    nothing in the request to infer it from. Derived from the registered
    redirect URI when that is set, since the two always share an origin.
    """
    explicit = os.environ.get("DSE_QUEUE_BOARD_URL")
    if explicit:
        return explicit.rstrip("/")
    redirect = os.environ.get("DSE_OIDC_REDIRECT_URI")
    if redirect:
        parsed = urllib.parse.urlparse(redirect)
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def _cookie_kwargs(request: Request) -> dict:
    """Flags for the session cookie, in one place so every issuer agrees.

    `domain` is what lets the two consoles share one login. The queue board and
    the admin panel are sibling hostnames, and a cookie scoped to one is never
    sent to the other; scoping it to the parent domain makes a single session
    authoritative for both. Left unset the cookie is host-only, which is the
    right default for a deployment that only serves one of them.

    `secure` follows the scheme the BROWSER used, not the one this process sees:
    TLS terminates at Traefik, so a hardcoded True would be correct in the
    cluster and would silently stop tests and local runs from keeping a session.
    """
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    kwargs = {"httponly": True, "samesite": "lax", "secure": proto == "https"}
    domain = os.environ.get("DSE_CONSOLE_COOKIE_DOMAIN")
    if domain:
        kwargs["domain"] = domain
    return kwargs


def _safe_next(request: Request, raw: str | None) -> str:
    """Where to land after login, refusing anywhere we do not control.

    An unchecked `next` turns the login into an open redirect: an attacker sends
    a victim to a real login URL on a real domain and the provider hands them
    back to the attacker's site, already authenticated-looking. Only a relative
    path or one of our own hostnames is accepted; anything else falls back to
    the board.
    """
    if not raw:
        return "/board"
    parsed = urllib.parse.urlparse(raw)
    if not parsed.scheme and not parsed.netloc:
        return raw if raw.startswith("/") else "/board"
    allowed = {request.headers.get("x-forwarded-host") or request.headers.get("host") or ""}
    console_url = os.environ.get("DSE_CONSOLE_URL")
    if console_url:
        allowed.add(urllib.parse.urlparse(console_url).netloc)
    if parsed.scheme in ("http", "https") and parsed.netloc in allowed - {""}:
        return raw
    return "/board"


def _redirect_uri(request: Request) -> str:
    """The callback URL, as the browser will see it.

    Built from the forwarded headers because the app sits behind Traefik and
    terminates TLS there: `request.url` would report http and the internal port,
    and the resulting redirect_uri would not match the one registered with the
    provider — which fails at the provider with an error the logs here never see.
    """
    override = os.environ.get("DSE_OIDC_REDIRECT_URI")
    if override:
        return override
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}/auth/callback"


def create_app(*, sender: SignalSender | None = None, verifier=None) -> FastAPI:
    """App factory. `sender` and `verifier` are injectable (tests pass a
    FakeSignalSender and an OIDCVerifier with the DevIdP JWKS)."""
    app = FastAPI(title="DSE Admin Queue Board (WS-F)")
    app.state.sender = sender or TemporalSignalSender(
        target=os.environ.get("DSE_TEMPORAL_TARGET", "localhost:7233")
    )
    app.state.verifier = verifier  # None => login disabled (must be injected)

    # ------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------
    def _issue_session(session: ConsoleSession) -> str:
        now = int(time.time())
        return jwt.encode(
            {
                "principal_id": session.principal_id,
                "sub": session.sso_subject,
                "tenant_id": session.tenant_id,
                "roles": session.roles,
                "iat": now,
                "exp": now + _SESSION_TTL,
            },
            _SESSION_SECRET,
            algorithm="HS256",
        )

    def current_operator(request: Request) -> ConsoleSession:
        token = request.cookies.get(_SESSION_COOKIE)
        if not token:
            raise HTTPException(status_code=401, detail="not authenticated (SSO required)")
        try:
            claims = jwt.decode(token, _SESSION_SECRET, algorithms=["HS256"])
        except jwt.InvalidTokenError:
            raise HTTPException(status_code=401, detail="invalid or expired session")
        principal_id = claims["principal_id"]
        # immediate offboarding: re-checked on every request
        if not is_console_active(principal_id):
            raise HTTPException(status_code=403, detail="account offboarded or expired")
        return ConsoleSession(
            principal_id=principal_id,
            sso_subject=claims.get("sub", ""),
            email=None,
            display_name=None,
            tenant_id=claims.get("tenant_id"),
            roles=claims.get("roles", []),
        )

    def _console(session: ConsoleSession) -> OperatorConsole:
        return OperatorConsole(app.state.sender, operator=session.principal_id)

    # ------------------------------------------------------------------
    # Login (SSO)
    # ------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        if request.cookies.get(_SESSION_COOKIE):
            return RedirectResponse("/board", status_code=302)
        return _page(
            "Sign in",
            # The paste-a-token form stays, but below the real button and marked
            # as what it is. It is how the tests and the dev IdP sign in, and
            # removing it would break them; leaving it as the FIRST thing an
            # operator sees is what made this console look unusable.
            """
            <h1>DSE Admin Queue Board</h1>
            <p><a href="/auth/login"><button type="button">Sign in with Microsoft</button></a></p>
            <details>
              <summary>Developer sign-in (paste an id_token)</summary>
              <p>For the dev IdP and tests. Production uses the button above.</p>
              <form method="post" action="/login">
                <textarea name="id_token" rows="4" cols="80" placeholder="id_token (JWT)"></textarea><br>
                <button type="submit">Sign in with a pasted token</button>
              </form>
            </details>
            """,
        )

    @app.post("/login")
    def login_post(request: Request, id_token: str = Form(...)):
        if app.state.verifier is None:
            raise HTTPException(status_code=503, detail="OIDC verifier not configured in this deployment")
        from ..sso import LoginDenied, InvalidToken, login as sso_login

        try:
            session = sso_login(app.state.verifier, id_token)
        except InvalidToken:
            raise HTTPException(status_code=401, detail="invalid id_token")
        except LoginDenied:
            raise HTTPException(status_code=403, detail="account offboarded or expired")
        resp = RedirectResponse("/board", status_code=302)
        resp.set_cookie(_SESSION_COOKIE, _issue_session(session), **_cookie_kwargs(request))
        return resp

    # ------------------------------------------------------------------
    # OIDC authorization-code flow
    #
    # The POST /login above expects an id_token to already exist, which is fine
    # for tests and for a token minted elsewhere but leaves a human with no way
    # in. These two routes are that way in: /auth/login sends the browser to the
    # provider, /auth/callback trades the returned code for a token and opens
    # the session.
    # ------------------------------------------------------------------
    @app.get("/auth/login")
    def auth_login(request: Request):
        cfg = _oidc_settings()
        if cfg is None:
            raise HTTPException(status_code=503, detail="OIDC is not configured in this deployment")
        endpoints = _oidc_endpoints(cfg["issuer"])
        if endpoints is None:
            raise HTTPException(status_code=503, detail="the identity provider is unreachable")

        # `state` defends the callback against CSRF; `nonce` binds the returned
        # id_token to THIS login attempt so a token captured elsewhere cannot be
        # replayed here. Both are echoed back by the provider and checked below.
        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(24)
        redirect_uri = _redirect_uri(request)
        # Carried in the signed transaction cookie rather than round-tripped
        # through the provider: it comes back untouched and unforgeable, and the
        # redirect_uri stays a single fixed value the provider can be told about.
        next_url = _safe_next(request, request.query_params.get("next"))

        params = {
            "client_id": cfg["client_id"],
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "response_mode": "query",
            "scope": "openid profile email",
            "state": state,
            "nonce": nonce,
        }
        resp = RedirectResponse(
            endpoints["authorization_endpoint"] + "?" + urllib.parse.urlencode(params),
            status_code=302,
        )
        now = int(time.time())
        resp.set_cookie(
            _OIDC_TX_COOKIE,
            jwt.encode(
                {"state": state, "nonce": nonce, "redirect_uri": redirect_uri,
                 "next": next_url, "iat": now, "exp": now + _OIDC_TX_TTL},
                _SESSION_SECRET,
                algorithm="HS256",
            ),
            # Same flags as the session cookie except its scope: this one is
            # only ever read back on the callback, which lands on this host, so
            # widening it to the parent domain would offer the login's CSRF
            # token to a service that has no use for it.
            #
            # `lax`, not `strict`: the callback arrives as a top-level navigation
            # from the provider's domain, and a strict cookie would not be sent
            # with it — the login would fail with a state mismatch every time.
            **{k: v for k, v in _cookie_kwargs(request).items() if k != "domain"},
            max_age=_OIDC_TX_TTL,
        )
        return resp

    @app.get("/auth/callback")
    def auth_callback(request: Request):
        cfg = _oidc_settings()
        if cfg is None or app.state.verifier is None:
            raise HTTPException(status_code=503, detail="OIDC is not configured in this deployment")

        params = request.query_params
        if params.get("error"):
            # The provider rejected the attempt (consent denied, app not
            # assigned). Surfacing its own words beats a generic failure.
            raise HTTPException(
                status_code=403,
                detail=f"{params.get('error')}: {params.get('error_description', '')}"[:300],
            )

        tx_raw = request.cookies.get(_OIDC_TX_COOKIE)
        if not tx_raw:
            raise HTTPException(status_code=400, detail="login expired or was started elsewhere")
        try:
            tx = jwt.decode(tx_raw, _SESSION_SECRET, algorithms=["HS256"])
        except jwt.InvalidTokenError:
            raise HTTPException(status_code=400, detail="invalid login transaction")

        # Constant-time compare: `state` is the CSRF defense and comparing it
        # with == leaks its content through timing.
        if not secrets.compare_digest(str(params.get("state", "")), str(tx.get("state", ""))):
            raise HTTPException(status_code=400, detail="state mismatch")

        code = params.get("code")
        if not code:
            raise HTTPException(status_code=400, detail="no authorization code returned")

        body = urllib.parse.urlencode({
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "code": code,
            "redirect_uri": tx["redirect_uri"],
            "grant_type": "authorization_code",
        }).encode()
        endpoints = _oidc_endpoints(cfg["issuer"])
        if endpoints is None:
            raise HTTPException(status_code=503, detail="the identity provider is unreachable")
        try:
            req = urllib.request.Request(
                endpoints["token_endpoint"], data=body, method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                tokens = json.load(r)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"token exchange failed: {type(exc).__name__}")

        id_token = tokens.get("id_token")
        if not id_token:
            raise HTTPException(status_code=502, detail="the provider returned no id_token")

        from ..sso import InvalidToken, LoginDenied, login as sso_login

        # Verify BEFORE trusting anything in it — the token arrived over TLS from
        # the provider, but signature/issuer/audience are still what make it
        # authoritative, and the nonce is what ties it to this attempt.
        try:
            claims = app.state.verifier.verify(id_token)
        except InvalidToken as e:
            raise HTTPException(status_code=401, detail=f"invalid id_token: {e}")
        if claims.raw.get("nonce") != tx.get("nonce"):
            raise HTTPException(status_code=400, detail="nonce mismatch")

        try:
            session = sso_login(app.state.verifier, id_token)
        except InvalidToken:
            raise HTTPException(status_code=401, detail="invalid id_token")
        except LoginDenied:
            raise HTTPException(status_code=403, detail="account offboarded or expired")

        # Re-validated on the way out, not just on the way in: the value is
        # signed, but a config change between the two legs could still widen
        # what counts as ours, and this is the redirect that actually fires.
        resp = RedirectResponse(_safe_next(request, tx.get("next")), status_code=302)
        resp.set_cookie(_SESSION_COOKIE, _issue_session(session), **_cookie_kwargs(request))
        resp.delete_cookie(_OIDC_TX_COOKIE)
        return resp

    @app.get("/auth/verify")
    def auth_verify(request: Request):
        """Session check for a reverse proxy guarding another host.

        The admin panel is a Next.js app with no notion of who is logged in.
        Rather than build a second, subtly different login into it, Traefik asks
        this route on every request to it and only forwards the ones this
        answers 200 to. One implementation of OIDC, one session, two surfaces.

        The 302 on failure is deliberate: a forward-auth response that is not
        2xx is returned to the browser as-is, so an unauthenticated visitor is
        sent to the login and back to the page they wanted instead of hitting a
        bare 401 with nothing to click.
        """
        try:
            session = current_operator(request)
        except HTTPException:
            # The forwarded headers describe the PROTECTED app, not this one, so
            # the login URL has to come from configuration. Deriving it from
            # them would send the browser to /auth/login on the panel's
            # hostname, where nothing serves it.
            base = _self_base_url()
            original = request.headers.get("x-forwarded-uri", "/")
            proto = request.headers.get("x-forwarded-proto", "https")
            host = request.headers.get("x-forwarded-host", "")
            login = f"{base}/auth/login"
            if host:
                login += "?" + urllib.parse.urlencode({"next": f"{proto}://{host}{original}"})
            return RedirectResponse(login, status_code=302)
        # Identity travels to the protected app as headers, so it can show who
        # is signed in without parsing or trusting the cookie itself.
        return Response(
            status_code=200,
            headers={
                "X-Auth-Principal": session.principal_id,
                "X-Auth-Subject": session.sso_subject or "",
                "X-Auth-Tenant": session.tenant_id or "",
            },
        )

    @app.get("/logout")
    def logout(request: Request):
        resp = RedirectResponse("/", status_code=302)
        # Same domain the cookie was set with, or the browser keeps it: a
        # delete that does not match scope is a no-op that looks like success.
        domain = os.environ.get("DSE_CONSOLE_COOKIE_DOMAIN")
        resp.delete_cookie(_SESSION_COOKIE, **({"domain": domain} if domain else {}))
        return resp

    # ------------------------------------------------------------------
    # Board — overview (tenants + budgets + kill switches)
    # ------------------------------------------------------------------
    @app.get("/board", response_class=HTMLResponse)
    def board(session: ConsoleSession = Depends(current_operator)):
        tenants = api.list_tenants()
        rows = []
        for t in tenants:
            b = api.get_tenant_budget(t)
            ks = "ON" if b.kill_switch_enabled else "off"
            rows.append(
                f"<tr><td><a href='/board/tenant/{_esc(t)}'>{_esc(t)}</a></td>"
                f"<td>{b.spent_usd} / {b.monthly_budget_usd}</td>"
                f"<td>{b.active_work_items} / {b.max_concurrent_work_items}</td>"
                f"<td>{ks}</td></tr>"
            )
        table = (
            "<table border=1 cellpadding=4><tr><th>tenant</th><th>spent / budget</th>"
            "<th>active / cap</th><th>kill switch</th></tr>" + "".join(rows) + "</table>"
        )
        gks = _global_kill_switch_form(session)
        return _page(
            "Board",
            f"<h1>Queue Board</h1><p>operator: <code>{_esc(session.principal_id)}</code> "
            f"| <a href='/logout'>sign out</a></p>{gks}<h2>Tenants</h2>{table}",
        )

    def _global_kill_switch_form(session):
        from .. import kill_switches

        enabled, reason = kill_switches.get_global_kill_switch()
        state = "ENABLED" if enabled else "disabled"
        target = "false" if enabled else "true"
        label = "Disable" if enabled else "ENABLE"
        return (
            f"<div style='border:1px solid #900;padding:6px'><b>GLOBAL kill switch: {state}</b> "
            f"({_esc(reason or '-')})<form method='post' action='/control/kill_switch_global' style='display:inline'>"
            f"<input type='hidden' name='enabled' value='{target}'>"
            f"<input name='reason' placeholder='reason'>"
            f"<button type='submit'>{label} global</button></form></div>"
        )

    # ------------------------------------------------------------------
    # Per-tenant board — every §9.3 state
    # ------------------------------------------------------------------
    @app.get("/board/tenant/{tenant_id}", response_class=HTMLResponse)
    def board_tenant(tenant_id: str, session: ConsoleSession = Depends(current_operator)):
        grouped = api.work_items_by_state(tenant_id=tenant_id)
        b = api.get_tenant_budget(tenant_id)
        sections = []
        for state in api.ALL_STATES:
            items = grouped.get(state, [])
            lis = []
            for wi in items:
                q = " [QUARANTINED]" if wi.quarantined else ""
                lis.append(
                    f"<li><a href='/work/{_esc(wi.work_item_id)}'>{_esc(wi.work_item_id)}</a> "
                    f"({_esc(wi.public_status)}){q} repo={_esc(wi.repo or '-')} pr={wi.pr_number or '-'}</li>"
                )
            body = "<ul>" + "".join(lis) + "</ul>" if lis else "<i>(empty)</i>"
            sections.append(f"<h3>{_esc(state)} <small>({len(items)})</small></h3>{body}")
        tenant_ks = _tenant_kill_switch_form(tenant_id, b)
        return _page(
            f"Tenant {tenant_id}",
            f"<p><a href='/board'>&larr; board</a></p><h1>{_esc(tenant_id)}</h1>"
            f"<p>budget: {b.spent_usd} / {b.monthly_budget_usd} | active: "
            f"{b.active_work_items} / {b.max_concurrent_work_items}</p>{tenant_ks}"
            + "".join(sections),
        )

    def _tenant_kill_switch_form(tenant_id, b):
        target = "false" if b.kill_switch_enabled else "true"
        label = "Disable" if b.kill_switch_enabled else "ENABLE"
        return (
            f"<form method='post' action='/control/kill_switch_tenant' style='display:inline'>"
            f"<input type='hidden' name='tenant_id' value='{_esc(tenant_id)}'>"
            f"<input type='hidden' name='enabled' value='{target}'>"
            f"<input name='reason' placeholder='reason'>"
            f"<button type='submit'>{label} tenant kill switch</button></form>"
        )

    # ------------------------------------------------------------------
    # Work item detail — audit trail + controls
    # ------------------------------------------------------------------
    @app.get("/work/{work_item_id}", response_class=HTMLResponse)
    def work_detail(work_item_id: str, session: ConsoleSession = Depends(current_operator)):
        items = api.list_work_items()
        wi = next((w for w in items if w.work_item_id == work_item_id), None)
        if wi is None:
            raise HTTPException(status_code=404, detail="work item not found")
        trail = api.get_audit_trail(work_item_id)
        trail_rows = "".join(
            f"<tr><td>{_esc(str(e['ts']))}</td><td>{_esc(e['actor'])}</td>"
            f"<td>{_esc(e['action'])}</td></tr>"
            for e in trail
        )
        controls = _controls(work_item_id, wi.tenant_id)
        return _page(
            f"Work {work_item_id}",
            f"<p><a href='/board/tenant/{_esc(wi.tenant_id)}'>&larr; {_esc(wi.tenant_id)}</a></p>"
            f"<h1>{_esc(work_item_id)}</h1>"
            f"<p>state: <b>{_esc(wi.status)}</b> ({_esc(wi.public_status)})"
            f"{' [QUARANTINED]' if wi.quarantined else ''}</p>{controls}"
            f"<h2>Audit trail</h2><table border=1 cellpadding=3>"
            f"<tr><th>ts</th><th>actor</th><th>action</th></tr>{trail_rows}</table>",
        )

    def _controls(wid, tenant):
        def btn(action, label, extra=""):
            return (
                f"<form method='post' action='/control/{action}' style='display:inline'>"
                f"<input type='hidden' name='work_item_id' value='{_esc(wid)}'>"
                f"<input type='hidden' name='tenant_id' value='{_esc(tenant)}'>{extra}"
                f"<button type='submit'>{label}</button></form> "
            )

        reason = "<input name='reason' placeholder='reason' size=12>"
        return (
            "<div style='border:1px solid #999;padding:6px'>"
            + btn("pause", "pause", reason)
            + btn("resume", "resume")
            + btn("cancel", "cancel", reason)
            + btn("retry_from_checkpoint", "retry")
            + btn("force_clarification", "force clarif", reason)
            + btn("escalate", "escalate", reason)
            + btn("quarantine", "quarantine", "<input name='reason' placeholder='reason' size=12 required>")
            + btn("release_quarantine", "release")
            + btn("reassign_model", "reassign model", "<input name='model' placeholder='model' size=12 required>")
            + btn("reassign_runtime", "reassign runtime", "<input name='runtime' placeholder='runtime' size=12 required>")
            + btn("kill_switch_task", "KILL task", reason)
            + "</div>"
        )

    # ------------------------------------------------------------------
    # Control endpoints (POST) — delegate to OperatorConsole
    # ------------------------------------------------------------------
    def _redirect_back(work_item_id: str | None, tenant_id: str | None):
        if work_item_id:
            return RedirectResponse(f"/work/{work_item_id}", status_code=302)
        if tenant_id:
            return RedirectResponse(f"/board/tenant/{tenant_id}", status_code=302)
        return RedirectResponse("/board", status_code=302)

    @app.post("/control/pause")
    def c_pause(work_item_id: str = Form(...), tenant_id: str = Form(...), reason: str = Form(None),
                session: ConsoleSession = Depends(current_operator)):
        _console(session).pause(work_item_id, tenant_id, reason=reason)
        return _redirect_back(work_item_id, tenant_id)

    @app.post("/control/resume")
    def c_resume(work_item_id: str = Form(...), tenant_id: str = Form(...),
                 session: ConsoleSession = Depends(current_operator)):
        _console(session).resume(work_item_id, tenant_id)
        return _redirect_back(work_item_id, tenant_id)

    @app.post("/control/cancel")
    def c_cancel(work_item_id: str = Form(...), tenant_id: str = Form(...), reason: str = Form(None),
                 session: ConsoleSession = Depends(current_operator)):
        _console(session).cancel(work_item_id, tenant_id, reason=reason)
        return _redirect_back(work_item_id, tenant_id)

    @app.post("/control/retry_from_checkpoint")
    def c_retry(work_item_id: str = Form(...), tenant_id: str = Form(...),
                session: ConsoleSession = Depends(current_operator)):
        _console(session).retry_from_checkpoint(work_item_id, tenant_id)
        return _redirect_back(work_item_id, tenant_id)

    @app.post("/control/force_clarification")
    def c_force(work_item_id: str = Form(...), tenant_id: str = Form(...), reason: str = Form(None),
                session: ConsoleSession = Depends(current_operator)):
        _console(session).force_clarification(work_item_id, tenant_id, reason=reason)
        return _redirect_back(work_item_id, tenant_id)

    @app.post("/control/escalate")
    def c_escalate(work_item_id: str = Form(...), tenant_id: str = Form(...), reason: str = Form(None),
                   session: ConsoleSession = Depends(current_operator)):
        _console(session).escalate(work_item_id, tenant_id, reason=reason)
        return _redirect_back(work_item_id, tenant_id)

    @app.post("/control/quarantine")
    def c_quarantine(work_item_id: str = Form(...), tenant_id: str = Form(...), reason: str = Form(...),
                     session: ConsoleSession = Depends(current_operator)):
        _console(session).quarantine(work_item_id, tenant_id, reason=reason)
        return _redirect_back(work_item_id, tenant_id)

    @app.post("/control/release_quarantine")
    def c_release(work_item_id: str = Form(...), tenant_id: str = Form(...),
                  session: ConsoleSession = Depends(current_operator)):
        _console(session).release_quarantine(work_item_id, tenant_id)
        return _redirect_back(work_item_id, tenant_id)

    @app.post("/control/reassign_model")
    def c_model(work_item_id: str = Form(...), tenant_id: str = Form(...), model: str = Form(...),
                session: ConsoleSession = Depends(current_operator)):
        _console(session).reassign_model(work_item_id, tenant_id, model=model)
        return _redirect_back(work_item_id, tenant_id)

    @app.post("/control/reassign_runtime")
    def c_runtime(work_item_id: str = Form(...), tenant_id: str = Form(...), runtime: str = Form(...),
                  session: ConsoleSession = Depends(current_operator)):
        _console(session).reassign_runtime(work_item_id, tenant_id, runtime=runtime)
        return _redirect_back(work_item_id, tenant_id)

    @app.post("/control/kill_switch_task")
    def c_kill_task(work_item_id: str = Form(...), tenant_id: str = Form(...), reason: str = Form(None),
                    session: ConsoleSession = Depends(current_operator)):
        _console(session).kill_switch_task(work_item_id, tenant_id, reason=reason)
        return _redirect_back(work_item_id, tenant_id)

    @app.post("/control/kill_switch_tenant")
    def c_kill_tenant(tenant_id: str = Form(...), enabled: str = Form(...), reason: str = Form(None),
                      session: ConsoleSession = Depends(current_operator)):
        _console(session).kill_switch_tenant(tenant_id, enabled=(enabled == "true"), reason=reason)
        return _redirect_back(None, tenant_id)

    @app.post("/control/kill_switch_global")
    def c_kill_global(enabled: str = Form(...), reason: str = Form(None),
                      session: ConsoleSession = Depends(current_operator)):
        _console(session).kill_switch_global(enabled=(enabled == "true"), reason=reason)
        return _redirect_back(None, None)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app


def _esc(s: str) -> str:
    import html

    return html.escape(str(s), quote=True)


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'><title>{_esc(title)} · DSE</title>"
        "<style>body{font-family:monospace;margin:1em;background:#fafafa}"
        "table{border-collapse:collapse}button{cursor:pointer}h3{margin:.4em 0}</style></head>"
        f"<body>{body}</body></html>"
    )
