"""WSA-E5-T1/T3 — Jira adapter: inbound (webhook, with the 4 intake defenses)
and outbound (single status comment via MutableCommentWriter + enqueueing of
per-ticket serialized transitions).

Inbound covers (UC2/UC5):
  - `jira:issue_created` / `jira:issue_updated` with the trigger label -> task_request
  - status transition into the configured approval column -> kind=approval
    (UC5 on the Jira surface) with an approved/rejected verdict marked on the payload
  - `comment_created` -> clarification (correlated by ticket key)

100% stateless adapter — same convention as the Slack/GitHub adapters. All of
the admission/correlation/defense logic comes from `ingest_gateway`; the
ingestion itself is shared with the poller in `adapter_jira.ingest`.
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from urllib.parse import urlsplit

from dse_audit import emit as audit_emit
from dse_contracts import mutable_comment
from dse_identity import resolve_principal
from fastapi import FastAPI, HTTPException, Request
from ingest_gateway import get_connection, resolve_tenant, verify_jira_signature
from pydantic import BaseModel

from . import events
from .backend import JiraCommentBackend, build_real_jira_client
from .comment_store import SURFACE, PgCommentStateStore
from .config import (
    get_plan_approved_status,
    get_plan_rejected_status,
    get_tenant_id,
    get_trigger_label,
    get_webhook_secret,
)
from .ingest import ingest_comment, ingest_status_approval, ingest_task_trigger
from .transitions import enqueue_transition

logger = logging.getLogger("adapter_jira")

app = FastAPI(title="dse-adapter-jira")


@lru_cache(maxsize=1)
def _dse_account_id() -> str | None:
    """accountId the DSE posts as — resolved once per process.

    Cached here rather than on the client because `build_real_jira_client()`
    returns a fresh instance each call, so a per-instance cache would issue one
    `/myself` request per webhook delivery.
    """
    return build_real_jira_client().self_account_id()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "adapter-jira"}


def _reject(reason: str) -> None:
    audit_emit(
        actor="system:adapter-jira",
        action="signature_rejected",
        tenant_id=get_tenant_id(),
        details={"reason": reason, "surface": "jira_webhook"},
    )
    raise HTTPException(status_code=401, detail=f"signature_verification_failed:{reason}")


def _site_from_payload(payload: dict) -> str | None:
    """Extracts the Jira site (scheme://host) from `issue.self` in order to
    resolve the tenant (WSA-E1-T5). E.g.: `https://acme.atlassian.net/rest/...`
    -> `https://acme.atlassian.net`."""
    self_url = (payload.get("issue") or {}).get("self", "")
    if not self_url:
        return None
    parts = urlsplit(self_url)
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return None


def _resolve_tenant_for(payload: dict) -> str:
    conn = get_connection()
    try:
        rt = resolve_tenant(conn, platform="jira", binding_key=_site_from_payload(payload))
        conn.commit()
        return rt.tenant_id
    finally:
        conn.close()


def _actor_of(user: dict) -> tuple[str, str, str | None]:
    """(account_id, resolved_principal, display_name) out of a Jira
    user/author object."""
    account_id = user.get("accountId", "")
    display = user.get("displayName")
    principal = resolve_principal("jira", account_id, display) if account_id else account_id
    return account_id, principal, display


def _status_targets_in_changelog(payload: dict, approved: str, rejected: str) -> list[str]:
    changelog = payload.get("changelog") or {}
    out = []
    for item in changelog.get("items", []):
        if item.get("field") == "status":
            to = item.get("toString")
            if to in (approved, rejected):
                out.append(to)
    return out


def _labeled_trigger_in_changelog(payload: dict, trigger_label: str) -> bool:
    changelog = payload.get("changelog") or {}
    for item in changelog.get("items", []):
        if item.get("field") == "labels":
            if trigger_label in (item.get("toString") or "").split():
                return True
    return False


@app.post("/jira/webhook")
async def jira_webhook(request: Request) -> dict:
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature")

    check = verify_jira_signature(webhook_secret=get_webhook_secret(), body=body, signature_header=signature)
    if not check.verified:
        _reject(check.reason)

    payload = json.loads(body)
    webhook_event = payload.get("webhookEvent", "")
    tenant_id = _resolve_tenant_for(payload)
    trigger_label = get_trigger_label()
    approved_status = get_plan_approved_status()
    rejected_status = get_plan_rejected_status()

    if webhook_event in ("jira:issue_created", "jira:issue_updated"):
        issue = payload["issue"]
        results = []

        # Task trigger: created with the label OR label added right now.
        trigger_now = events.has_trigger_label(issue, trigger_label)
        if webhook_event == "jira:issue_created" and trigger_now:
            should_trigger = True
        elif webhook_event == "jira:issue_updated":
            should_trigger = trigger_now and _labeled_trigger_in_changelog(payload, trigger_label)
        else:
            should_trigger = False

        if should_trigger:
            account_id, principal, display = _actor_of(payload.get("user") or (issue.get("fields", {}).get("reporter") or {}))
            results.append(
                ingest_task_trigger(
                    issue,
                    tenant_id=tenant_id,
                    actor_account_id=account_id,
                    resolved_principal=principal,
                    display_name=display,
                )
            )

        # Status transition -> plan approval (UC5).
        for target in _status_targets_in_changelog(payload, approved_status, rejected_status):
            account_id, principal, display = _actor_of(payload.get("user") or {})
            verdict = "approved" if target == approved_status else "rejected"
            results.append(
                ingest_status_approval(
                    issue,
                    tenant_id=tenant_id,
                    target_status=target,
                    verdict=verdict,
                    route="re_plan" if verdict == "rejected" else None,
                    actor_account_id=account_id,
                    resolved_principal=principal,
                    display_name=display,
                )
            )

        if not results:
            return {"ok": True, "path": "ignored_no_trigger"}
        return {"ok": True, "path": "processed", "results": results}

    if webhook_event == "comment_created":
        issue = payload["issue"]
        comment = payload["comment"]
        account_id, principal, display = _actor_of(comment.get("author") or {})
        body_text = comment.get("body")
        if not isinstance(body_text, str):
            from .backend import _flatten_adf

            body_text = _flatten_adf(body_text)
        result = ingest_comment(
            tenant_id=tenant_id,
            key=events.ticket_key(issue),
            comment_id=str(comment["id"]),
            body=body_text,
            actor_account_id=account_id,
            resolved_principal=principal,
            display_name=display,
            # The webhook fires on the DSE's own comments too — same feedback
            # loop as the poller's, arriving by a different door.
            self_account_id=_dse_account_id(),
        )
        return {"ok": True, **result}

    return {"ok": True, "path": "ignored_unhandled_event", "webhook_event": webhook_event}


class StatusCommentRequest(BaseModel):
    work_item_id: str
    ticket_key: str
    body: str
    actor: str


@app.post("/internal/status-comment")
def upsert_status_comment(req: StatusCommentRequest) -> dict:
    """WSA-E5-T3: exactly 1 status comment per ticket, edited in-place, via the
    SAME MutableCommentWriter as the other adapters (new Jira backend)."""
    client = build_real_jira_client()
    backend = JiraCommentBackend(client)
    store = PgCommentStateStore()
    writer = mutable_comment.MutableCommentWriter(backend, store, SURFACE)

    comment_ref = writer.upsert(req.work_item_id, {"ticket_key": req.ticket_key}, req.body)

    audit_emit(
        actor=req.actor,
        action="status_comment_upserted",
        tenant_id=get_tenant_id(),
        work_item_id=req.work_item_id,
        details={"surface": SURFACE, "ticket_key": req.ticket_key},
    )
    return {"ok": True, "comment_ref": comment_ref}


class TransitionRequest(BaseModel):
    work_item_id: str
    ticket_key: str
    target_status: str
    dedup_key: str
    actor: str
    tenant_id: str | None = None


@app.post("/internal/transition")
def enqueue_transition_endpoint(req: TransitionRequest) -> dict:
    """WSA-E5-T3: enqueues a status transition (serialized per ticket by the
    TransitionWorker). Idempotent by `dedup_key`."""
    tenant_id = req.tenant_id or get_tenant_id()
    conn = get_connection()
    try:
        is_new = enqueue_transition(
            conn,
            tenant_id=tenant_id,
            ticket_key=req.ticket_key,
            target_status=req.target_status,
            dedup_key=req.dedup_key,
            work_item_id=req.work_item_id,
        )
        audit_emit(
            actor=req.actor,
            action="jira_transition_enqueued" if is_new else "jira_transition_enqueue_duplicate",
            tenant_id=tenant_id,
            work_item_id=req.work_item_id,
            details={"ticket_key": req.ticket_key, "target_status": req.target_status, "dedup_key": req.dedup_key},
            conn=conn,
        )
        conn.commit()
        return {"ok": True, "enqueued": is_new}
    finally:
        conn.close()
