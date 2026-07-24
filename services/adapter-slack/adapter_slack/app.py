"""WSA-E3-T1/T2 — adapter Slack: inbound (Events API + Interactivity) e
outbound (status message única, editada in-place). Adapter 100% stateless:
nenhum estado vive no processo — tudo (comment_ref, kill switch, allowlist,
work_items) é lido/escrito no Postgres compartilhado a cada request.

Pipeline inbound, na ordem (as "4 defesas" do WSA-E2):
  1. verify_slack_signature (HMAC + janela de replay)      -> 401 se falhar
  2. content_snapshot congelado do próprio payload (TOCTOU) -> automático
  3. sanitize_content (unicode invisível + redação de secret)
  4. idempotência: event_id determinístico -> dedup em admit_work_item/
     record_signal_event via UNIQUE constraint
depois disso: correlate() decide Path A (new_task) vs Path B (signal) vs
unauthorized (steering allowlist).
"""
from __future__ import annotations

import json
import logging

from dse_audit import emit as audit_emit
from dse_contracts import mutable_comment
from dse_identity import resolve_principal
from fastapi import FastAPI, HTTPException, Request
from ingest_gateway import (
    AdmissionBlocked,
    admit_work_item,
    correlate,
    get_connection,
    is_authorized_to_steer,
    record_signal_event,
    resolve_tenant,
    resolve_repo,
    sanitize_content,
    verify_slack_signature,
)
from pydantic import BaseModel

from .backend import SlackCommentBackend, approval_blocks, build_real_slack_client, repo_select_blocks
from .comment_store import SURFACE, PgCommentStateStore
from .config import get_slack_bot_token, get_slack_signing_secret, get_tenant_id
from .events import (
    build_event_from_app_mention,
    build_event_from_block_action,
    build_event_from_thread_message,
    build_repo_select_signal_event,
    parse_slack_approval,
)

logger = logging.getLogger("adapter_slack")

app = FastAPI(title="dse-adapter-slack")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "adapter-slack"}


def _reject(reason: str, *, surface: str) -> None:
    audit_emit(
        actor="system:adapter-slack",
        action="signature_rejected",
        tenant_id=get_tenant_id(),
        details={"reason": reason, "surface": surface},
    )
    raise HTTPException(status_code=401, detail=f"signature_verification_failed:{reason}")


def _resolve_tenant_for(team_id: str | None) -> str:
    """WSA-E1-T5 — resolve o tenant a partir do workspace Slack (`team_id`)
    via `tenant_platform_bindings`. Binding ausente cai para `DSE_TENANT_ID`
    com audit row de aviso (fallback single-tenant documentado)."""
    conn = get_connection()
    try:
        rt = resolve_tenant(conn, platform="slack", binding_key=team_id)
        conn.commit()
        return rt.tenant_id
    finally:
        conn.close()


def _distinct_repos_for_tenant(conn, tenant_id: str) -> list[str]:
    """Repos distintos do tenant — espelha a fonte que resolve_repo Rung 4/5
    considerou ambígua (mesmo WHERE, sem filtro de plataforma). Ordenado ->
    Block Kit determinístico."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT repo FROM repo_bindings "
            "WHERE tenant_id = %s AND repo IS NOT NULL ORDER BY repo",
            (tenant_id,),
        )
        return [r[0] for r in cur.fetchall()]


def _base_branch_for_repo(conn, tenant_id: str, repo: str) -> str:
    """base_branch do binding do repo escolhido (o repo ambíguo não trouxe um).
    Default 'main' (convenção do resolve_repo Rung 1)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT base_branch FROM repo_bindings "
            "WHERE tenant_id = %s AND repo = %s AND base_branch IS NOT NULL LIMIT 1",
            (tenant_id, repo),
        )
        row = cur.fetchone()
    return row[0] if row and row[0] else "main"


def _handle_conversation_event(conv_event, *, principal: str, tenant_id: str,
                               extra_payload: dict | None = None) -> dict:
    channel = conv_event.source_ref["channel"]
    sanitized = sanitize_content(conv_event.content_snapshot)

    conn = get_connection()
    try:
        result = correlate(conn, tenant_id=tenant_id, event=conv_event, requester_principal=principal)

        if result.kind == "unauthorized":
            conn.commit()
            return {"ok": True, "path": "unauthorized"}

        if result.kind == "signal":
            record_signal_event(
                conv_event,
                tenant_id=tenant_id,
                channel=channel,
                work_item_id=result.work_item_id,
                sanitized_content=sanitized,
                extra_payload=extra_payload,
                conn=conn,
            )
            return {"ok": True, "path": "signal", "work_item_id": result.work_item_id}

        # Path A: new_task — C2 (relatório 07): resolve o repo pela cascata
        # (override explícito no texto → binding do canal → default do tenant).
        # Sem resolução, repo=None e o gate de clarificação pergunta (nunca
        # adivinha). O texto usado é o SANITIZADO (nunca o cru).
        repo, base_branch = resolve_repo(
            conn, tenant_id=tenant_id, platform="slack",
            signals={"text": sanitized, "channel": channel},
        )
        try:
            work_item_id = admit_work_item(
                conv_event,
                tenant_id=tenant_id,
                source="slack",
                channel=channel,
                repo=repo,
                base_branch=base_branch,
                requester_principal=principal,
                sanitized_content=sanitized,
                conn=conn,
            )
        except AdmissionBlocked:
            return {"ok": True, "path": "blocked_kill_switch"}

        if result.provenance_work_item_id:
            audit_emit(
                actor=principal,
                action="work_item_provenance_link",
                tenant_id=tenant_id,
                work_item_id=work_item_id,
                details={"previous_work_item_id": result.provenance_work_item_id},
            )

        return {"ok": True, "path": "new_task", "work_item_id": work_item_id}
    finally:
        conn.close()


@app.post("/slack/events")
async def slack_events(request: Request) -> dict:
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp")
    signature = request.headers.get("X-Slack-Signature")

    check = verify_slack_signature(
        signing_secret=get_slack_signing_secret(),
        timestamp_header=timestamp,
        body=body,
        signature_header=signature,
    )
    if not check.verified:
        _reject(check.reason, surface="slack_events")

    payload = json.loads(body)

    if payload.get("type") == "url_verification":
        return {"challenge": payload["challenge"]}

    if payload.get("type") != "event_callback":
        return {"ok": True}

    event = payload["event"]
    event_type = event.get("type")
    user_id = event.get("user")
    if not user_id:
        return {"ok": True}  # eventos sem user (ex.: bot_message) ignorados na Fase 1

    principal = resolve_principal("slack", user_id)
    tenant_id = _resolve_tenant_for(payload.get("team_id"))

    if event_type == "app_mention":
        conv_event = build_event_from_app_mention(event, resolved_principal=principal)
    elif event_type == "message" and not event.get("subtype") and event.get("thread_ts"):
        conv_event = build_event_from_thread_message(event, resolved_principal=principal)
    else:
        return {"ok": True}  # tipo de evento não coberto na Fase 1

    return _handle_conversation_event(conv_event, principal=principal, tenant_id=tenant_id)


@app.post("/slack/interactions")
async def slack_interactions(request: Request) -> dict:
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp")
    signature = request.headers.get("X-Slack-Signature")

    check = verify_slack_signature(
        signing_secret=get_slack_signing_secret(),
        timestamp_header=timestamp,
        body=body,
        signature_header=signature,
    )
    if not check.verified:
        _reject(check.reason, surface="slack_interactions")

    form = await request.form()
    payload = json.loads(form["payload"])

    if payload.get("type") != "block_actions":
        return {"ok": True}

    user_id = payload["user"]["id"]
    principal = resolve_principal("slack", user_id)
    team_id = (payload.get("team") or {}).get("id") or payload.get("user", {}).get("team_id")
    tenant_id = _resolve_tenant_for(team_id)
    action = payload["actions"][0]

    # Seletor de repo (clarificação de repo ambíguo): NÃO é aprovação. Endereça
    # pelo work_item_id do block_id (não por correlação — o status-comment é
    # postado FORA da thread). O repo+base_branch viram o marcador
    # `repo=X branch=Y` no content -> o dispatcher extrai (regex C4) ->
    # SIGNAL clarification_answer -> o workflow repõe input.repo/base_branch.
    # Efeito idêntico a digitar `repo=org/x branch=main` na thread.
    if action.get("action_id") == "dse_repo_select":
        block_id = action.get("block_id", "")
        work_item_id = block_id.split(":", 1)[1] if ":" in block_id else block_id
        repo = (action.get("selected_option") or {}).get("value")
        if not work_item_id or not repo:
            return {"ok": True, "path": "repo_select_noop"}
        # Paridade de segurança com o gate de clarification_answer do correlate
        # (steering allowlist). Sem isto qualquer um no canal escolheria o repo.
        if not is_authorized_to_steer(tenant_id, principal):
            audit_emit(actor=principal, action="steering_rejected_unauthorized",
                       tenant_id=tenant_id,
                       details={"kind": "repo_select", "work_item_id": work_item_id})
            return {"ok": True, "path": "unauthorized"}
        channel = payload["channel"]["id"]
        conn = get_connection()
        try:
            content = f"repo={repo} branch={_base_branch_for_repo(conn, tenant_id, repo)}"
            conv_event = build_repo_select_signal_event(
                payload, action, resolved_principal=principal, content=content
            )
            record_signal_event(
                conv_event, tenant_id=tenant_id, channel=channel,
                work_item_id=work_item_id, sanitized_content=content, conn=conn,
            )
            conn.commit()  # persiste o ingest_event p/ o dispatcher drenar
        except AdmissionBlocked:
            return {"ok": True, "path": "blocked_kill_switch"}
        finally:
            conn.close()
        return {"ok": True, "path": "repo_selected", "work_item_id": work_item_id, "repo": repo}

    conv_event = build_event_from_block_action(payload, resolved_principal=principal)

    # C1 (relatório 07): deriva o verdict/route do botão para marcadores
    # DETERMINÍSTICOS — sem isto o dispatcher default para `approved` e um
    # "reject" aprovaria o plano silenciosamente (bug de segurança do gate).
    verdict, route = parse_slack_approval(action.get("action_id", ""), action.get("value", ""))
    extra_payload: dict = {"approval_verdict": verdict}
    if route:
        extra_payload["approval_route"] = route

    return _handle_conversation_event(
        conv_event, principal=principal, tenant_id=tenant_id, extra_payload=extra_payload
    )


class StatusCommentRequest(BaseModel):
    work_item_id: str
    channel: str
    body: str
    actor: str  # principal resolvido de quem/o-que disparou a atualização (ex.: "system:orchestrator")
    status: str | None = None  # Fase B: quando 'awaiting_plan_approval', monta os botões


@app.post("/internal/status-comment")
def upsert_status_comment(req: StatusCommentRequest) -> dict:
    """WSA-E3-T2: exatamente 1 mensagem de status por WorkItem, editada
    in-place — chamado pelo orchestrator (WS-B) a cada transição de estado
    relevante. Usa `MutableCommentWriter` compartilhado (dse_contracts).

    Fase B (relatório 07): no status `awaiting_plan_approval` a mensagem sai
    com Block Kit (botões Approve/Reject) — a mesma mensagem mutável, só que
    interativa. Os cliques voltam por /slack/interactions (verdict via C1)."""
    client = build_real_slack_client(get_slack_bot_token())
    backend = SlackCommentBackend(client)
    store = PgCommentStateStore()
    writer = mutable_comment.MutableCommentWriter(backend, store, SURFACE)

    surface_ref = {"channel": req.channel}
    if req.status == "awaiting_plan_approval":
        surface_ref["blocks"] = approval_blocks(req.body)
    elif req.status == "awaiting_repo_selection":
        # Repo ambíguo: oferece um static_select com os repos do tenant. Com < 2
        # repos degrada p/ texto puro (nada a escolher -> só a pergunta de texto).
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT tenant_id FROM work_items WHERE id = %s", (req.work_item_id,))
                row = cur.fetchone()
            tenant_id = row[0] if row else get_tenant_id()
            repos = _distinct_repos_for_tenant(conn, tenant_id)
            conn.commit()
        finally:
            conn.close()
        if len(repos) >= 2:
            surface_ref["blocks"] = repo_select_blocks(req.work_item_id, repos, req.body)
    comment_ref = writer.upsert(req.work_item_id, surface_ref, req.body)

    audit_emit(
        actor=req.actor,
        action="status_comment_upserted",
        tenant_id=get_tenant_id(),
        work_item_id=req.work_item_id,
        details={"surface": SURFACE, "channel": req.channel},
    )
    return {"ok": True, "comment_ref": comment_ref}
