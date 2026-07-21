"""WS-A adapter Microsoft Teams (PROVISÃO, Fase 4) — inbound (Activity ->
ConversationEvent pelas 4 defesas) e outbound (status message única, editada
in-place, via `MutableCommentWriter` com backend Teams).

Adapter 100% stateless — mesma convenção de Slack/GitHub/Jira. Toda a lógica de
admissão/correlação/defesas vem de `ingest_gateway`; a normalização Teams vive em
`adapter_teams.events`.

Pipeline inbound, na ordem (as "4 defesas" do WSA-E2):
  1. verify_teams_signature (HMAC do outgoing webhook)   -> 401 se falhar
  2. content_snapshot congelado do próprio payload (TOCTOU) -> automático em events
  3. sanitize_content (unicode invisível + redação de secret)
  4. idempotência: event_id determinístico -> dedup em admit/record_signal

Guard de ATIVAÇÃO: enquanto a fundação não expõe `Platform.teams`, o endpoint
verifica a assinatura (defesa real) e então retorna 501 `teams_not_activated`
ANTES de qualquer escrita (evita violar os CHECKs de plataforma de work_items/
identity_links). Ligar Teams é 1 linha no enum + activation.sql — sem tocar aqui.
"""
from __future__ import annotations

import json
import logging

from dse_audit import emit as audit_emit
from dse_contracts import mutable_comment
from dse_identity import resolve_principal
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from ingest_gateway import (
    AdmissionBlocked,
    admit_work_item,
    correlate,
    get_connection,
    record_signal_event,
    resolve_tenant,
    sanitize_content,
    verify_teams_signature,
)
from pydantic import BaseModel

from . import events
from .backend import TeamsCommentBackend, build_real_teams_client
from .comment_store import SURFACE, PgCommentStateStore
from .config import (
    get_default_service_url,
    get_teams_app_id,
    get_teams_app_password,
    get_teams_shared_secret,
    get_tenant_id,
)
from .platform_compat import TeamsNotActivated, is_activated

logger = logging.getLogger("adapter_teams")

app = FastAPI(title="dse-adapter-teams")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "adapter-teams", "activated": is_activated()}


def _reject(reason: str) -> None:
    audit_emit(
        actor="system:adapter-teams",
        action="signature_rejected",
        tenant_id=get_tenant_id(),
        details={"reason": reason, "surface": "teams_webhook"},
    )
    raise HTTPException(status_code=401, detail=f"signature_verification_failed:{reason}")


def _resolve_tenant_for(activity: dict) -> str:
    conn = get_connection()
    try:
        rt = resolve_tenant(conn, platform="teams", binding_key=events.aad_tenant_id(activity))
        conn.commit()
        return rt.tenant_id
    finally:
        conn.close()


def _handle_conversation_event(conv_event, *, principal: str, tenant_id: str) -> dict:
    conv_id = conv_event.source_ref["conversation_id"]
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
                channel=conv_id,
                work_item_id=result.work_item_id,
                sanitized_content=sanitized,
                conn=conn,
            )
            return {"ok": True, "path": "signal", "work_item_id": result.work_item_id}

        try:
            work_item_id = admit_work_item(
                conv_event,
                tenant_id=tenant_id,
                source="teams",
                channel=conv_id,
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


@app.post("/teams/messages")
async def teams_messages(request: Request):
    body = await request.body()
    authorization = request.headers.get("Authorization")

    # Defesa #1: assinatura (HMAC do outgoing webhook). Sempre verificada,
    # mesmo antes da ativação — é a fronteira de confiança.
    check = verify_teams_signature(
        shared_secret=get_teams_shared_secret(), body=body, authorization_header=authorization
    )
    if not check.verified:
        _reject(check.reason)

    activity = json.loads(body)
    if activity.get("type") != "message":
        return {"ok": True, "path": "ignored_non_message"}

    # Guard de ativação ANTES de qualquer escrita (evita violar CHECKs de
    # plataforma). Falha limpa e explicativa (P6), com audit (P8).
    if not is_activated():
        audit_emit(
            actor="system:adapter-teams",
            action="teams_inbound_not_activated",
            tenant_id=get_tenant_id(),
            details={"conversation_id": events.conversation_id(activity), "event_id": events.compute_event_id(activity)},
        )
        return JSONResponse(status_code=501, content={"ok": False, "error": str(TeamsNotActivated())})

    # --- Caminho pós-ativação (exercitado quando a fundação expõe Platform.teams) ---
    user_id, display = events.actor_of(activity)
    principal = resolve_principal("teams", user_id, display)
    tenant_id = _resolve_tenant_for(activity)
    conv_event = events.build_conversation_event(activity, resolved_principal=principal)
    return _handle_conversation_event(conv_event, principal=principal, tenant_id=tenant_id)


class StatusCommentRequest(BaseModel):
    work_item_id: str
    conversation_id: str
    service_url: str
    body: str
    actor: str  # principal resolvido de quem/o-que disparou (ex.: "system:orchestrator")


@app.post("/internal/status-comment")
def upsert_status_comment(req: StatusCommentRequest) -> dict:
    """Exatamente 1 mensagem de status por WorkItem, editada in-place — chamado
    pelo orchestrator (WS-B) a cada transição relevante. Usa a MESMA
    `MutableCommentWriter` dos outros adapters (backend Teams).

    O outbound NÃO depende do enum `Platform.teams` (a surface é só a string
    "teams") — logo é plenamente funcional/testável já, com `FakeTeamsClient`."""
    service_url = req.service_url or get_default_service_url()
    client = build_real_teams_client(get_teams_app_id(), get_teams_app_password())
    backend = TeamsCommentBackend(client)
    store = PgCommentStateStore()
    writer = mutable_comment.MutableCommentWriter(backend, store, SURFACE)

    comment_ref = writer.upsert(
        req.work_item_id,
        {"conversation_id": req.conversation_id, "service_url": service_url},
        req.body,
    )

    audit_emit(
        actor=req.actor,
        action="status_comment_upserted",
        tenant_id=get_tenant_id(),
        work_item_id=req.work_item_id,
        details={"surface": SURFACE, "conversation_id": req.conversation_id},
    )
    return {"ok": True, "comment_ref": comment_ref}
