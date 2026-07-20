"""Núcleo de ingestão do adapter Jira, COMPARTILHADO entre o webhook
(`app.py`) e o poller de fallback (`poller.py`) — WSA-E5-T1/T2.

Ambas as vias chamam exatamente estas funções, que constroem o mesmo
`ConversationEvent` (com `message_id` derivado do estado do issue, ver
`events.py`) e passam pela mesma via idempotente de `ingest_gateway`
(`admit_work_item`/`record_signal_event`, dedup por `event_id`). É isso que
garante "webhook + poller nunca duplicam": os dois produzem o mesmo
`event_id`, e o segundo a chegar deduplica.

Transação: cada função abre sua própria conexão e delega o commit para
`admit_work_item`/`record_signal_event` (que comitam quando recebem `conn`),
mesma convenção dos handlers do adapter-github.
"""
from __future__ import annotations

from typing import Any

from dse_audit import emit as audit_emit
from ingest_gateway import (
    AdmissionBlocked,
    admit_work_item,
    correlate,
    get_connection,
    record_signal_event,
    sanitize_content,
)

from . import events


def ingest_task_trigger(
    issue: dict[str, Any],
    *,
    tenant_id: str,
    actor_account_id: str,
    resolved_principal: str,
    display_name: str | None = None,
) -> dict:
    """Issue com a trigger label -> task_request (Path A) ou signal
    idempotente se já houver WorkItem ativo para o ticket. Mirror do
    `_handle_task_creating_event` do adapter-github."""
    ev = events.build_task_event(
        issue, actor_account_id=actor_account_id, resolved_principal=resolved_principal, display_name=display_name
    )
    sanitized = sanitize_content(ev.content_snapshot)
    channel = events.project_key(issue)
    conn = get_connection()
    try:
        result = correlate(conn, tenant_id=tenant_id, event=ev, requester_principal=resolved_principal)

        if result.kind == "unauthorized":
            conn.commit()
            return {"ok": True, "path": "unauthorized"}

        if result.kind == "signal":
            record_signal_event(
                ev,
                tenant_id=tenant_id,
                channel=channel,
                work_item_id=result.work_item_id,
                sanitized_content=sanitized,
                conn=conn,
            )
            return {"ok": True, "path": "signal", "work_item_id": result.work_item_id}

        try:
            work_item_id = admit_work_item(
                ev,
                tenant_id=tenant_id,
                source="jira",
                channel=channel,
                requester_principal=resolved_principal,
                sanitized_content=sanitized,
                conn=conn,
            )
        except AdmissionBlocked:
            return {"ok": True, "path": "blocked_kill_switch"}

        if result.provenance_work_item_id:
            audit_emit(
                actor=resolved_principal,
                action="work_item_provenance_link",
                tenant_id=tenant_id,
                work_item_id=work_item_id,
                details={"previous_work_item_id": result.provenance_work_item_id},
            )
        return {"ok": True, "path": "new_task", "work_item_id": work_item_id}
    finally:
        conn.close()


def ingest_comment(
    *,
    tenant_id: str,
    key: str,
    comment_id: str,
    body: str,
    actor_account_id: str,
    resolved_principal: str,
    display_name: str | None = None,
) -> dict:
    """Comentário no issue -> signal (clarificação) para um WorkItem ativo;
    sem WorkItem ativo, ignorado (comentário em issue que não é tarefa DSE)."""
    ev = events.build_comment_event(
        key=key,
        comment_id=comment_id,
        body=body,
        actor_account_id=actor_account_id,
        resolved_principal=resolved_principal,
        display_name=display_name,
    )
    sanitized = sanitize_content(ev.content_snapshot)
    conn = get_connection()
    try:
        result = correlate(conn, tenant_id=tenant_id, event=ev, requester_principal=resolved_principal)

        if result.kind == "unauthorized":
            conn.commit()
            return {"ok": True, "path": "unauthorized"}

        if result.kind == "signal":
            record_signal_event(
                ev,
                tenant_id=tenant_id,
                channel=events.project_key({"key": key}),
                work_item_id=result.work_item_id,
                sanitized_content=sanitized,
                conn=conn,
            )
            return {"ok": True, "path": "signal", "work_item_id": result.work_item_id}

        conn.rollback()
        audit_emit(
            actor=resolved_principal,
            action="jira_comment_ignored_no_active_work_item",
            tenant_id=tenant_id,
            details={"ticket_key": key, "comment_id": comment_id},
        )
        return {"ok": True, "path": "ignored_no_active_work_item"}
    finally:
        conn.close()


def ingest_status_approval(
    issue: dict[str, Any],
    *,
    tenant_id: str,
    target_status: str,
    verdict: str,
    route: str | None,
    actor_account_id: str,
    resolved_principal: str,
    display_name: str | None = None,
) -> dict:
    """Transição para a coluna de aprovação/rejeição configurada -> kind=
    approval (UC5). Marca `approval_verdict`/`approval_route` no payload
    (marcadores determinísticos lidos pelo dispatcher em WSA-E6-T3). Sem
    WorkItem ativo para o ticket, ignorado."""
    ev = events.build_status_approval_event(
        issue,
        target_status=target_status,
        actor_account_id=actor_account_id,
        resolved_principal=resolved_principal,
        display_name=display_name,
    )
    extra = {"approval_verdict": verdict}
    if route:
        extra["approval_route"] = route
    conn = get_connection()
    try:
        result = correlate(conn, tenant_id=tenant_id, event=ev, requester_principal=resolved_principal)

        if result.kind == "signal":
            record_signal_event(
                ev,
                tenant_id=tenant_id,
                channel=events.project_key(issue),
                work_item_id=result.work_item_id,
                sanitized_content=sanitize_content(ev.content_snapshot),
                extra_payload=extra,
                conn=conn,
            )
            return {"ok": True, "path": "signal_approval", "work_item_id": result.work_item_id, "verdict": verdict}

        conn.rollback()
        audit_emit(
            actor=resolved_principal,
            action="jira_status_transition_ignored_no_active_work_item",
            tenant_id=tenant_id,
            details={"ticket_key": events.ticket_key(issue), "target_status": target_status},
        )
        return {"ok": True, "path": "ignored_no_active_work_item"}
    finally:
        conn.close()
