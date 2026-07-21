"""WSA-E4-T1/T2 — adapter GitHub: inbound (webhooks da GitHub App) e
outbound (status comment único, editado in-place, sob identidade GitHub
App). Adapter 100% stateless — mesma convenção do adapter-slack.

Regra central de WSA-E4-T1: comentário em PR (via `issue_comment` numa
issue que é PR, ou via `pull_request_review_comment`) NUNCA cria um
WorkItem novo — só correlaciona (`signal`) a um WorkItem ativo por número de
PR/issue, ou é ignorado (com audit) se não houver nenhum ativo.
"""
from __future__ import annotations

import json
import logging

from dse_audit import emit as audit_emit
from dse_contracts import EventKind, mutable_comment
from dse_identity import resolve_principal
from fastapi import FastAPI, HTTPException, Request
from ingest_gateway import (
    AdmissionBlocked,
    admit_work_item,
    correlate,
    get_connection,
    record_signal_event,
    resolve_tenant,
    sanitize_content,
    verify_github_signature,
)
from pydantic import BaseModel

from .backend import GithubCommentBackend, build_real_github_client
from .comment_store import SURFACE, PgCommentStateStore
from .config import get_bot_mention_login, get_task_label, get_tenant_id, get_webhook_secret
from .events import (
    build_event_from_issue_assigned_or_labeled,
    build_event_from_issue_comment,
    build_event_from_pr_merged,
    build_event_from_pr_review_comment,
)

logger = logging.getLogger("adapter_github")

app = FastAPI(title="dse-adapter-github")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "adapter-github"}


def _reject(reason: str) -> None:
    audit_emit(
        actor="system:adapter-github",
        action="signature_rejected",
        tenant_id=get_tenant_id(),
        details={"reason": reason, "surface": "github_webhook"},
    )
    raise HTTPException(status_code=401, detail=f"signature_verification_failed:{reason}")


def _resolve_tenant_for(payload: dict) -> str:
    """WSA-E1-T5 — resolve o tenant a partir da installation da GitHub App
    (`payload["installation"]["id"]`) via `tenant_platform_bindings`. Binding
    ausente cai para `DSE_TENANT_ID` com audit row de aviso (fallback
    single-tenant documentado)."""
    installation_id = (payload.get("installation") or {}).get("id")
    conn = get_connection()
    try:
        rt = resolve_tenant(
            conn,
            platform="github",
            binding_key=str(installation_id) if installation_id is not None else None,
        )
        conn.commit()
        return rt.tenant_id
    finally:
        conn.close()


def _handle_task_creating_event(conv_event, *, principal: str, tenant_id: str, base_branch: str | None = None) -> dict:
    """Path usado por eventos que PODEM legitimamente abrir um WorkItem
    novo (issues assigned/labeled, comentário com menção numa issue comum).
    """
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
                channel=conv_event.source_ref["repo"],
                work_item_id=result.work_item_id,
                sanitized_content=sanitized,
                conn=conn,
            )
            return {"ok": True, "path": "signal", "work_item_id": result.work_item_id}

        if conv_event.kind != EventKind.task_request:
            # new_task só é permitido quando o evento é genuinamente um
            # gatilho de criação (assigned/labeled/@menção) — um comentário
            # comum sem menção e sem WorkItem ativo é ignorado.
            conn.rollback()
            audit_emit(
                actor=principal,
                action="comment_ignored_no_mention_no_active_work_item",
                tenant_id=tenant_id,
                details={"repo": conv_event.source_ref["repo"], "number": conv_event.source_ref["number"]},
            )
            return {"ok": True, "path": "ignored_no_mention"}

        try:
            work_item_id = admit_work_item(
                conv_event,
                tenant_id=tenant_id,
                source="github",
                channel=conv_event.source_ref["repo"],
                requester_principal=principal,
                repo=conv_event.source_ref["repo"],
                base_branch=base_branch,
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


def _handle_pr_comment_event(conv_event, *, principal: str, tenant_id: str) -> dict:
    """Path usado por comentários em PR (issue_comment numa PR ou
    pull_request_review_comment) — NUNCA cria WorkItem novo (WSA-E4-T1)."""
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
                channel=conv_event.source_ref["repo"],
                work_item_id=result.work_item_id,
                sanitized_content=sanitized,
                conn=conn,
            )
            return {"ok": True, "path": "signal", "work_item_id": result.work_item_id}

        # result.kind == "new_task" -> ZERO WorkItems novos a partir de
        # comentário de PR, por design (mesmo sem match).
        conn.rollback()
        audit_emit(
            actor=principal,
            action="review_comment_ignored_no_active_work_item",
            tenant_id=tenant_id,
            details={"repo": conv_event.source_ref["repo"], "number": conv_event.source_ref["number"]},
        )
        return {"ok": True, "path": "ignored_no_active_work_item"}
    finally:
        conn.close()


def _handle_merge_event(conv_event, *, principal: str, tenant_id: str, pr_number: int) -> dict:
    """WSA-E4-T3 — pull_request merged: correlaciona por número de PR ao
    WorkItem ATIVO e dispara `merged_by_human` (via marcador determinístico no
    payload, lido pelo dispatcher). NUNCA cria WorkItem novo; sem WorkItem
    ativo correspondente, é ignorado com audit (rota documentada)."""
    conn = get_connection()
    try:
        result = correlate(conn, tenant_id=tenant_id, event=conv_event, requester_principal=principal)

        if result.kind == "signal":
            record_signal_event(
                conv_event,
                tenant_id=tenant_id,
                channel=conv_event.source_ref["repo"],
                work_item_id=result.work_item_id,
                extra_payload={"merged_by_human": True, "merged_by": principal, "pr_number": pr_number},
                conn=conn,
            )
            return {"ok": True, "path": "signal_merged_by_human", "work_item_id": result.work_item_id}

        # new_task (sem match) OU match terminal -> merge não dispara nada.
        conn.rollback()
        audit_emit(
            actor=principal,
            action="merge_ignored_no_active_work_item",
            tenant_id=tenant_id,
            details={"repo": conv_event.source_ref["repo"], "number": pr_number},
        )
        return {"ok": True, "path": "ignored_no_active_work_item"}
    finally:
        conn.close()


@app.post("/github/webhook")
async def github_webhook(request: Request) -> dict:
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    delivery_id = request.headers.get("X-GitHub-Delivery", "")
    event_type = request.headers.get("X-GitHub-Event", "")

    check = verify_github_signature(webhook_secret=get_webhook_secret(), body=body, signature_header=signature)
    if not check.verified:
        _reject(check.reason)

    payload = json.loads(body)
    action = payload.get("action")
    tenant_id = _resolve_tenant_for(payload)

    if event_type == "issues" and action in ("assigned", "labeled"):
        if action == "labeled":
            label_name = payload.get("label", {}).get("name", "")
            if label_name != get_task_label():
                return {"ok": True, "path": "ignored_label"}
        sender = payload["sender"]["login"]
        principal = resolve_principal("github", sender, sender)
        conv_event = build_event_from_issue_assigned_or_labeled(
            payload, delivery_id=delivery_id, resolved_principal=principal
        )
        # S6 (Fase 5): issues do GitHub nao tem base_branch; o default do repo
        # vem no proprio payload do webhook (repository.default_branch) — sem
        # chamada extra a API. Preenche o WorkItem para o gate de completude
        # (S2) nao pedir clarificacao a toa e o clone (S4) saber a branch base.
        default_branch = (payload.get("repository") or {}).get("default_branch")
        return _handle_task_creating_event(
            conv_event, principal=principal, tenant_id=tenant_id, base_branch=default_branch
        )

    if event_type == "issue_comment" and action == "created":
        sender = payload["comment"]["user"]["login"]
        principal = resolve_principal("github", sender, sender)
        conv_event, is_pr_comment = build_event_from_issue_comment(payload, resolved_principal=principal)

        if is_pr_comment:
            return _handle_pr_comment_event(conv_event, principal=principal, tenant_id=tenant_id)

        mention = f"@{get_bot_mention_login()}".lower()
        if mention in conv_event.content_snapshot.lower():
            conv_event = conv_event.model_copy(update={"kind": EventKind.task_request})
        return _handle_task_creating_event(conv_event, principal=principal, tenant_id=tenant_id)

    if event_type == "pull_request_review_comment" and action == "created":
        sender = payload["comment"]["user"]["login"]
        principal = resolve_principal("github", sender, sender)
        conv_event = build_event_from_pr_review_comment(payload, resolved_principal=principal)
        return _handle_pr_comment_event(conv_event, principal=principal, tenant_id=tenant_id)

    if event_type == "pull_request" and action == "closed":
        # WSA-E4-T3: só o merge dispara signal. PR fechado SEM merge NÃO
        # dispara nada (rota documentada).
        pr = payload["pull_request"]
        pr_number = pr["number"]
        repo = payload["repository"]["full_name"]
        if not pr.get("merged"):
            audit_emit(
                actor="system:adapter-github",
                action="pr_closed_without_merge_ignored",
                tenant_id=tenant_id,
                details={"repo": repo, "number": pr_number},
            )
            return {"ok": True, "path": "ignored_pr_closed_unmerged"}

        merged_by_login = (pr.get("merged_by") or {}).get("login") or payload.get("sender", {}).get("login", "")
        principal = resolve_principal("github", merged_by_login, merged_by_login)
        merge_sha = pr.get("merge_commit_sha") or delivery_id
        conv_event = build_event_from_pr_merged(payload, resolved_principal=principal, merge_sha=merge_sha)
        return _handle_merge_event(conv_event, principal=principal, tenant_id=tenant_id, pr_number=pr_number)

    return {"ok": True, "path": "ignored_unhandled_event_type"}


class StatusCommentRequest(BaseModel):
    work_item_id: str
    repo: str
    issue_number: int
    body: str
    actor: str


@app.post("/internal/status-comment")
def upsert_status_comment(req: StatusCommentRequest) -> dict:
    """WSA-E4-T2: exatamente 1 status comment por issue/PR, editado
    in-place, sob identidade GitHub App (`build_real_github_client` usa um
    installation access token, nunca PAT pessoal)."""
    client = build_real_github_client()
    backend = GithubCommentBackend(client)
    store = PgCommentStateStore()
    writer = mutable_comment.MutableCommentWriter(backend, store, SURFACE)

    comment_ref = writer.upsert(req.work_item_id, {"repo": req.repo, "number": req.issue_number}, req.body)

    audit_emit(
        actor=req.actor,
        action="status_comment_upserted",
        tenant_id=get_tenant_id(),
        work_item_id=req.work_item_id,
        details={"surface": SURFACE, "repo": req.repo, "issue_number": req.issue_number},
    )
    return {"ok": True, "comment_ref": comment_ref}
