"""WSF-E6-T3 — UI utilitária mínima do queue board (server-rendered, SEM design
system) na porta 8890. Mostra todos os estados da máquina §9.3 por tenant e
dispara os controles de operador. Acesso via SSO (WSF-E3-T3).

Deliberadamente feia e sem framework de front: HTML montado em Python, tabelas
cruas, forms POST. O objetivo é operação, não estética. Cada controle é um form
que chama um endpoint que delega ao `OperatorConsole` (audit + signal).

Autenticação: cookie de sessão assinado (HS256) emitido após um login SSO
bem-sucedido (`dse_platform.sso.login`). Em dev, o login aceita um `id_token`
colado (mintado pelo `DevIdP` — ver README/gaps); em produção, troca-se o
handler de login por um redirect OIDC real ao IdP do cliente (o resto — verify,
sessão, offboarding — não muda). Cada request re-checa se a conta ainda está
ativa (offboarding tem efeito imediato).
"""
from __future__ import annotations

import os
import time

import jwt
from fastapi import Depends, FastAPI, Form, HTTPException, Request
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


def create_app(*, sender: SignalSender | None = None, verifier=None) -> FastAPI:
    """Fábrica do app. `sender` e `verifier` são injetáveis (testes passam um
    FakeSignalSender e um OIDCVerifier com JWKS do DevIdP)."""
    app = FastAPI(title="DSE Admin Queue Board (WS-F)")
    app.state.sender = sender or TemporalSignalSender(
        target=os.environ.get("DSE_TEMPORAL_TARGET", "localhost:7233")
    )
    app.state.verifier = verifier  # None => login desabilitado (deve ser injetado)

    # ------------------------------------------------------------------
    # Sessão
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
            raise HTTPException(status_code=401, detail="não autenticado (SSO exigido)")
        try:
            claims = jwt.decode(token, _SESSION_SECRET, algorithms=["HS256"])
        except jwt.InvalidTokenError:
            raise HTTPException(status_code=401, detail="sessão inválida/expirada")
        principal_id = claims["principal_id"]
        # offboarding imediato: re-checa a cada request
        if not is_console_active(principal_id):
            raise HTTPException(status_code=403, detail="conta offboardada/expirada")
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
            "Login",
            """
            <h1>DSE Admin Queue Board</h1>
            <p>Acesso via SSO (OIDC). Em dev, cole um <code>id_token</code> emitido pelo IdP de dev.</p>
            <form method="post" action="/login">
              <textarea name="id_token" rows="4" cols="80" placeholder="id_token (JWT)"></textarea><br>
              <button type="submit">Entrar</button>
            </form>
            """,
        )

    @app.post("/login")
    def login_post(request: Request, id_token: str = Form(...)):
        if app.state.verifier is None:
            raise HTTPException(status_code=503, detail="OIDC verifier não configurado neste deployment")
        from ..sso import LoginDenied, InvalidToken, login as sso_login

        try:
            session = sso_login(app.state.verifier, id_token)
        except InvalidToken:
            raise HTTPException(status_code=401, detail="id_token inválido")
        except LoginDenied:
            raise HTTPException(status_code=403, detail="conta offboardada/expirada")
        resp = RedirectResponse("/board", status_code=302)
        resp.set_cookie(_SESSION_COOKIE, _issue_session(session), httponly=True, samesite="lax")
        return resp

    @app.get("/logout")
    def logout():
        resp = RedirectResponse("/", status_code=302)
        resp.delete_cookie(_SESSION_COOKIE)
        return resp

    # ------------------------------------------------------------------
    # Board — visão geral (tenants + budgets + kill switches)
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
            "<table border=1 cellpadding=4><tr><th>tenant</th><th>gasto/budget</th>"
            "<th>ativos/cap</th><th>kill switch</th></tr>" + "".join(rows) + "</table>"
        )
        gks = _global_kill_switch_form(session)
        return _page(
            "Board",
            f"<h1>Queue Board</h1><p>operador: <code>{_esc(session.principal_id)}</code> "
            f"| <a href='/logout'>sair</a></p>{gks}<h2>Tenants</h2>{table}",
        )

    def _global_kill_switch_form(session):
        from .. import kill_switches

        enabled, reason = kill_switches.get_global_kill_switch()
        state = "ATIVADO" if enabled else "desativado"
        target = "false" if enabled else "true"
        label = "Desativar" if enabled else "ATIVAR"
        return (
            f"<div style='border:1px solid #900;padding:6px'><b>Kill switch GLOBAL: {state}</b> "
            f"({_esc(reason or '-')})<form method='post' action='/control/kill_switch_global' style='display:inline'>"
            f"<input type='hidden' name='enabled' value='{target}'>"
            f"<input name='reason' placeholder='motivo'>"
            f"<button type='submit'>{label} global</button></form></div>"
        )

    # ------------------------------------------------------------------
    # Board por tenant — todos os estados §9.3
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
                q = " [QUARENTENA]" if wi.quarantined else ""
                lis.append(
                    f"<li><a href='/work/{_esc(wi.work_item_id)}'>{_esc(wi.work_item_id)}</a> "
                    f"({_esc(wi.public_status)}){q} repo={_esc(wi.repo or '-')} pr={wi.pr_number or '-'}</li>"
                )
            body = "<ul>" + "".join(lis) + "</ul>" if lis else "<i>(vazio)</i>"
            sections.append(f"<h3>{_esc(state)} <small>({len(items)})</small></h3>{body}")
        tenant_ks = _tenant_kill_switch_form(tenant_id, b)
        return _page(
            f"Tenant {tenant_id}",
            f"<p><a href='/board'>&larr; board</a></p><h1>{_esc(tenant_id)}</h1>"
            f"<p>budget: {b.spent_usd} / {b.monthly_budget_usd} | ativos: "
            f"{b.active_work_items} / {b.max_concurrent_work_items}</p>{tenant_ks}"
            + "".join(sections),
        )

    def _tenant_kill_switch_form(tenant_id, b):
        target = "false" if b.kill_switch_enabled else "true"
        label = "Desativar" if b.kill_switch_enabled else "ATIVAR"
        return (
            f"<form method='post' action='/control/kill_switch_tenant' style='display:inline'>"
            f"<input type='hidden' name='tenant_id' value='{_esc(tenant_id)}'>"
            f"<input type='hidden' name='enabled' value='{target}'>"
            f"<input name='reason' placeholder='motivo'>"
            f"<button type='submit'>{label} tenant kill switch</button></form>"
        )

    # ------------------------------------------------------------------
    # Detalhe do work item — trilha de audit + controles
    # ------------------------------------------------------------------
    @app.get("/work/{work_item_id}", response_class=HTMLResponse)
    def work_detail(work_item_id: str, session: ConsoleSession = Depends(current_operator)):
        items = api.list_work_items()
        wi = next((w for w in items if w.work_item_id == work_item_id), None)
        if wi is None:
            raise HTTPException(status_code=404, detail="work item não encontrado")
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
            f"<p>estado: <b>{_esc(wi.status)}</b> ({_esc(wi.public_status)})"
            f"{' [QUARENTENA]' if wi.quarantined else ''}</p>{controls}"
            f"<h2>Trilha de audit</h2><table border=1 cellpadding=3>"
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

        reason = "<input name='reason' placeholder='motivo' size=12>"
        return (
            "<div style='border:1px solid #999;padding:6px'>"
            + btn("pause", "pause", reason)
            + btn("resume", "resume")
            + btn("cancel", "cancel", reason)
            + btn("retry_from_checkpoint", "retry")
            + btn("force_clarification", "force clarif", reason)
            + btn("escalate", "escalate", reason)
            + btn("quarantine", "quarantine", "<input name='reason' placeholder='motivo' size=12 required>")
            + btn("release_quarantine", "release")
            + btn("reassign_model", "reassign model", "<input name='model' placeholder='modelo' size=12 required>")
            + btn("reassign_runtime", "reassign runtime", "<input name='runtime' placeholder='runtime' size=12 required>")
            + btn("kill_switch_task", "KILL task", reason)
            + "</div>"
        )

    # ------------------------------------------------------------------
    # Endpoints de controle (POST) — delegam ao OperatorConsole
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
