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
    record_signal_event,
    sanitize_content,
    verify_slack_signature,
)
from pydantic import BaseModel

from .backend import SlackCommentBackend, build_real_slack_client
from .comment_store import SURFACE, PgCommentStateStore
from .config import get_slack_bot_token, get_slack_signing_secret, get_tenant_id
from .events import (
    build_event_from_app_mention,
    build_event_from_block_action,
    build_event_from_thread_message,
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


def _handle_conversation_event(conv_event, *, principal: str) -> dict:
    channel = conv_event.source_ref["channel"]
    sanitized = sanitize_content(conv_event.content_snapshot)

    conn = get_connection()
    try:
        result = correlate(conn, tenant_id=get_tenant_id(), event=conv_event, requester_principal=principal)

        if result.kind == "unauthorized":
            conn.commit()
            return {"ok": True, "path": "unauthorized"}

        if result.kind == "signal":
            record_signal_event(
                conv_event,
                tenant_id=get_tenant_id(),
                channel=channel,
                work_item_id=result.work_item_id,
                sanitized_content=sanitized,
                conn=conn,
            )
            return {"ok": True, "path": "signal", "work_item_id": result.work_item_id}

        # Path A: new_task
        try:
            work_item_id = admit_work_item(
                conv_event,
                tenant_id=get_tenant_id(),
                source="slack",
                channel=channel,
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
                tenant_id=get_tenant_id(),
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

    if event_type == "app_mention":
        conv_event = build_event_from_app_mention(event, resolved_principal=principal)
    elif event_type == "message" and not event.get("subtype") and event.get("thread_ts"):
        conv_event = build_event_from_thread_message(event, resolved_principal=principal)
    else:
        return {"ok": True}  # tipo de evento não coberto na Fase 1

    return _handle_conversation_event(conv_event, principal=principal)


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
    conv_event = build_event_from_block_action(payload, resolved_principal=principal)

    return _handle_conversation_event(conv_event, principal=principal)


class StatusCommentRequest(BaseModel):
    work_item_id: str
    channel: str
    body: str
    actor: str  # principal resolvido de quem/o-que disparou a atualização (ex.: "system:orchestrator")


@app.post("/internal/status-comment")
def upsert_status_comment(req: StatusCommentRequest) -> dict:
    """WSA-E3-T2: exatamente 1 mensagem de status por WorkItem, editada
    in-place — chamado pelo orchestrator (WS-B) a cada transição de estado
    relevante. Usa `MutableCommentWriter` compartilhado (dse_contracts)."""
    client = build_real_slack_client(get_slack_bot_token())
    backend = SlackCommentBackend(client)
    store = PgCommentStateStore()
    writer = mutable_comment.MutableCommentWriter(backend, store, SURFACE)

    comment_ref = writer.upsert(req.work_item_id, {"channel": req.channel}, req.body)

    audit_emit(
        actor=req.actor,
        action="status_comment_upserted",
        tenant_id=get_tenant_id(),
        work_item_id=req.work_item_id,
        details={"surface": SURFACE, "channel": req.channel},
    )
    return {"ok": True, "comment_ref": comment_ref}
