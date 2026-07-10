"""WSA-E6-T1 — Correlação Path A/B.

`correlate(...)` decide, para um `ConversationEvent` recebido, se ele abre
uma tarefa nova (Path A, "new_task" -> `admit_work_item`) ou é um sinal para
uma tarefa já em andamento (Path B, "signal" -> `SignalWorkflow`, decidido
por quem chama: o próprio dispatcher ou o WS-B via Temporal client).

Lookup determinístico por `source_ref` (thread_ts/PR number/ticket) contra
`work_items` com status NÃO-terminal. Convenção de `source_ref` usada para o
match (ver adapters): Slack `{"channel":..., "thread_ts":...}`, GitHub
`{"repo":..., "number":...}` (mesmo número serve para issue e PR — a API do
GitHub compartilha o namespace de números entre os dois).

Regra de steering allowlist (WSA-E6-T2a): comentário do tipo `steering` ou
`review_comment` sobre uma tarefa ativa só vira "signal" se
`is_authorized_to_steer` autorizar o principal; senão retorna "unauthorized"
e emite `dse_audit.emit(action="steering_rejected_unauthorized")`.
`clarification_answer`/`approval` não passam por este gate — são respostas
esperadas dentro do próprio fluxo (o bot perguntou, o usuário respondeu),
não uma injeção de direção não solicitada.

Evento correlacionado a um WorkItem já em estado TERMINAL (done/failed):
por definição não pode receber um sinal (o workflow já encerrou) — a regra
documentada é permitir a criação de um WorkItem NOVO com um link de
proveniência ao anterior (`provenance_work_item_id`), gravado pelo caller
nos `details` do audit row de admissão.
"""
from __future__ import annotations

import json
from typing import Any, Literal, NamedTuple

from dse_audit import emit as audit_emit
from dse_contracts import ConversationEvent, EventKind, WorkItemStatus

from .steering import is_authorized_to_steer

CorrelationKind = Literal["new_task", "signal", "unauthorized"]

_TERMINAL_STATUSES = {WorkItemStatus.done.value, WorkItemStatus.failed.value}

# Kinds que representam "alguém injetando direção nova" numa tarefa ativa —
# passam pelo gate de steering allowlist. `clarification_answer`/`approval`
# são respostas esperadas do próprio fluxo e não passam por este gate.
_STEERING_GATED_KINDS = {EventKind.steering, EventKind.review_comment}


class CorrelationResult(NamedTuple):
    kind: CorrelationKind
    work_item_id: str | None
    provenance_work_item_id: str | None = None


def correlate(
    conn,
    *,
    tenant_id: str,
    event: ConversationEvent,
    requester_principal: str,
    correlation_ref: dict[str, Any] | None = None,
) -> CorrelationResult:
    """Nota de transação: quando o resultado é "unauthorized", esta função
    grava a audit row usando `conn` mas NÃO comita — o caller (adapter)
    controla a fronteira de transação e deve chamar `conn.commit()` (mesma
    convenção de `dse_audit.emit(conn=...)`)."""
    ref = correlation_ref if correlation_ref is not None else event.source_ref

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, status FROM work_items
            WHERE tenant_id = %s AND source_ref @> %s::jsonb
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (tenant_id, json.dumps(ref)),
        )
        row = cur.fetchone()

    if row is None:
        return CorrelationResult("new_task", None)

    matched_id, status = row

    if status in _TERMINAL_STATUSES:
        # Regra documentada: WorkItem terminal não recebe sinal — vira
        # WorkItem novo com proveniência ao anterior.
        return CorrelationResult("new_task", None, provenance_work_item_id=matched_id)

    if event.kind in _STEERING_GATED_KINDS:
        if not is_authorized_to_steer(tenant_id, requester_principal):
            audit_emit(
                actor=requester_principal,
                action="steering_rejected_unauthorized",
                tenant_id=tenant_id,
                work_item_id=matched_id,
                details={"event_id": event.event_id, "kind": event.kind.value, "source_ref": ref},
                conn=conn,
            )
            return CorrelationResult("unauthorized", matched_id)

    return CorrelationResult("signal", matched_id)
