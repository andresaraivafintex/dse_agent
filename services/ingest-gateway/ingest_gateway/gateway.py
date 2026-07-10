"""WSA-E1-T3 — Gateway transacional (outbox).

`admit_work_item` grava um WorkItem novo + seu ingest_event na MESMA
transação Postgres. Idempotência de nível de tarefa: `idempotency_key` e o
próprio `work_item_id` são derivados deterministicamente de
`ConversationEvent.event_id` (que já é um sha256 de
platform+thread+message) — reentregas do mesmo webhook (mesmo event_id)
convergem para o mesmo work_item_id via `ON CONFLICT ... DO NOTHING`, nunca
duplicando linha.

Kill switch de canal é checado ANTES de qualquer INSERT: evento de canal
desligado não cria WorkItem nem ingest_event, e gera
`dse_audit.emit(action="admission_blocked_kill_switch")`.
"""
from __future__ import annotations

import json
from typing import Any

from dse_audit import emit as audit_emit
from dse_contracts import ConversationEvent

from .kill_switch import is_channel_killed
from .db import get_connection


class AdmissionBlocked(Exception):
    """Levantada quando o kill switch de canal/tenant bloqueia a admissão.
    Não é um erro de infraestrutura — é uma decisão determinística válida
    (P1), o caller (adapter) deve tratá-la retornando 200 sem side effects
    além do audit row já emitido por esta função."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _payload_json(event: ConversationEvent, sanitized_content: str | None) -> str:
    """`ingest_events.payload`: o `ConversationEvent` completo (com o
    `content_snapshot` ORIGINAL intacto, congelado pela defesa TOCTOU —
    WSA-E2-T2) mais, se fornecido, `sanitized_content` — a versão que passou
    pela sanitização (WSA-E2-T3) e é a que deve seguir para qualquer estágio
    que envolva um modelo. Nunca sobrescreve `content_snapshot`."""
    data = event.model_dump(mode="json")
    if sanitized_content is not None:
        data["sanitized_content"] = sanitized_content
    return json.dumps(data)


def admit_work_item(
    event: ConversationEvent,
    *,
    tenant_id: str,
    source: str,
    channel: str,
    requester_principal: str,
    repo: str | None = None,
    base_branch: str | None = None,
    data_class: str = "internal",
    sanitized_content: str | None = None,
    conn=None,
) -> str:
    """Retorna o `work_item_id` (novo ou já existente, se reentrega).

    Levanta `AdmissionBlocked` se o kill switch do canal/tenant estiver
    ativo — nesse caso NENHUM WorkItem/ingest_event é criado.
    """
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()

    try:
        killed, reason = is_channel_killed(conn, tenant_id, channel)
        if killed:
            audit_emit(
                actor="system:ingest-gateway",
                action="admission_blocked_kill_switch",
                tenant_id=tenant_id,
                details={"channel": channel, "reason": reason, "event_id": event.event_id},
                conn=conn,
            )
            conn.commit()
            raise AdmissionBlocked(reason or "kill_switch_active")

        work_item_id = f"wi_{event.event_id}"
        idempotency_key = event.event_id

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO work_items
                    (id, tenant_id, source, source_ref, repo, base_branch,
                     requester, data_class, idempotency_key)
                VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                (
                    work_item_id,
                    tenant_id,
                    source,
                    json.dumps(event.source_ref),
                    repo,
                    base_branch,
                    requester_principal,
                    data_class,
                    idempotency_key,
                ),
            )
            cur.execute(
                """
                INSERT INTO ingest_events (work_item_id, event_id, kind, payload)
                VALUES (%s, %s, %s, %s::jsonb)
                ON CONFLICT (event_id) DO NOTHING
                """,
                (
                    work_item_id,
                    event.event_id,
                    event.kind.value,
                    _payload_json(event, sanitized_content),
                ),
            )

        audit_emit(
            actor=requester_principal,
            action="work_item_admitted",
            tenant_id=tenant_id,
            work_item_id=work_item_id,
            details={"source": source, "channel": channel, "event_id": event.event_id},
            conn=conn,
        )

        conn.commit()
        return work_item_id
    except AdmissionBlocked:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        if owns_conn:
            conn.close()


def record_signal_event(
    event: ConversationEvent,
    *,
    tenant_id: str,
    channel: str,
    work_item_id: str,
    sanitized_content: str | None = None,
    conn=None,
) -> bool:
    """Grava o ConversationEvent de um sinal (Path B — correlacionado a um
    WorkItem já existente) no mesmo outbox `ingest_events`, SEM criar um
    `work_items` novo. O dispatcher (WSA-E1-T3) drena esta tabela e decide
    `start_workflow` (kind == task_request, cria o workflow) vs
    `signal_workflow` (demais kinds, sinaliza o workflow já em andamento) —
    ver `ingest_gateway.dispatcher`.

    Retorna True se este é o primeiro registro deste `event_id` (novo sinal
    a ser dispatchado), False se já havia sido registrado antes (reentrega
    de webhook — dedup por `event_id` UNIQUE).
    """
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()

    try:
        killed, reason = is_channel_killed(conn, tenant_id, channel)
        if killed:
            audit_emit(
                actor="system:ingest-gateway",
                action="admission_blocked_kill_switch",
                tenant_id=tenant_id,
                work_item_id=work_item_id,
                details={"channel": channel, "reason": reason, "event_id": event.event_id},
                conn=conn,
            )
            conn.commit()
            raise AdmissionBlocked(reason or "kill_switch_active")

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ingest_events (work_item_id, event_id, kind, payload)
                VALUES (%s, %s, %s, %s::jsonb)
                ON CONFLICT (event_id) DO NOTHING
                RETURNING id
                """,
                (work_item_id, event.event_id, event.kind.value, _payload_json(event, sanitized_content)),
            )
            is_new = cur.fetchone() is not None

        audit_emit(
            actor="system:ingest-gateway",
            action="signal_recorded" if is_new else "signal_duplicate_ignored",
            tenant_id=tenant_id,
            work_item_id=work_item_id,
            details={"channel": channel, "event_id": event.event_id, "kind": event.kind.value},
            conn=conn,
        )
        conn.commit()
        return is_new
    except AdmissionBlocked:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        if owns_conn:
            conn.close()
