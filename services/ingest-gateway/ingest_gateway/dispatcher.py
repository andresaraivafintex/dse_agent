"""WSA-E1-T3 — Dispatcher outbox.

Drena `ingest_events` não processados com `SELECT ... FOR UPDATE SKIP
LOCKED` (permite N processos/threads concorrentes drenando a mesma fila sem
duplicar nem perder — é o núcleo do chaos test NFR-01 do lado do intake) e,
para cada evento:

  - `kind == "task_request"` -> `Temporal.start_workflow(WORKFLOW_TYPE,
    work_item_id, id=work_item_id, task_queue=TASK_QUEUE)`. Temporal rejeita
    workflow_id duplicado (`WorkflowAlreadyStartedError`) — tratado como
    sucesso idempotente (nunca re-lançado).
  - qualquer outro kind (`clarification_answer`/`approval`/`review_comment`/
    `steering`) -> sinal a um workflow já em andamento
    (`WorkflowHandle.signal(SIGNAL_NAME, payload)`), correlacionado por
    `ingest_events.work_item_id` (já resolvido por `correlate()` /
    `record_signal_event()` no momento da ingestão).

`processed=true` só é marcado DEPOIS do StartWorkflow/Signal confirmar (ou
da exceção de duplicado) — nunca antes, para não perder eventos em caso de
crash entre o dequeue e a confirmação Temporal.

Fase 2 (WSA-E6-T3): o mapa fixo `kind -> signal` da Fase 1 foi substituído por
`_route_signal(status, kind, payload)`, que consulta `work_items.status` para
escolher o gate certo — um `kind=approval` com status `awaiting_plan_approval`
dispara `SIGNAL_PLAN_APPROVAL` (gate de plano, WSB-E3-T2), com status
`pr_ready`/`review_feedback` dispara `SIGNAL_REVIEW_COMMENT`, e com qualquer
outro status é declinado com audit row (nunca adivinha — P6). O webhook de
merge (WSA-E4-T3) chega com o marcador `merged_by_human` no payload e dispara
`SIGNAL_MERGED_BY_HUMAN`. `clarification_answer`/`review_comment` preservam o
comportamento da Fase 1. Todos os nomes de signal vêm de
`dse_contracts.constants` (fonte única).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, NamedTuple

from dse_audit import emit as audit_emit
from dse_contracts import TASK_QUEUE, WORKFLOW_TYPE
from dse_contracts.constants import (
    SIGNAL_CLARIFICATION_ANSWER,
    SIGNAL_MERGED_BY_HUMAN,
    SIGNAL_PLAN_APPROVAL,
    SIGNAL_REVIEW_COMMENT,
)
from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError

from .db import get_connection

logger = logging.getLogger("ingest_gateway.dispatcher")

_TASK_REQUEST_KIND = "task_request"

# WSA-E6-T3 — status do WorkItem em que um `kind=approval` roteia para o gate
# de aprovação de PLANO (Fase 2, WSB-E3-T2) em vez do gate de review.
_STATUS_AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"
_STATUS_PR_READY = "pr_ready"
# Estados em que um approval sobre o PR ainda é um sinal de review legítimo
# (o revisor pode aprovar depois de já ter pedido mudanças).
_APPROVAL_REVIEW_STATES = {_STATUS_PR_READY, "review_feedback"}


class DispatchOutcome:
    STARTED = "started"
    DEDUPED_ALREADY_STARTED = "deduped_already_started"
    SIGNALED = "signaled"
    SIGNAL_FAILED = "signal_failed"
    IGNORED_NOT_A_DECISION = "ignored_not_a_decision"
    # WSA-E6-T3: kind=approval chegou com um status de WorkItem em que o
    # dispatcher não sabe (deterministicamente) para qual gate rotear — NUNCA
    # adivinha (P6). Consumido (processed=true) com audit row, não re-tentado.
    DECLINED_UNEXPECTED_STATUS = "declined_unexpected_status"


_CHANGES_REQUESTED_STATES = {"changes_requested", "request_changes", "changes-requested"}


class SignalRoute(NamedTuple):
    signal_name: str | None  # None => não sinaliza
    payload: dict[str, Any] | None
    reason: str  # rótulo para audit/observabilidade


def _content_of(raw_payload: dict[str, Any]) -> str:
    return raw_payload.get("sanitized_content") or raw_payload.get("content_snapshot", "")


def _actor_principal(raw_payload: dict[str, Any]) -> str | None:
    actor = raw_payload.get("actor") or {}
    return actor.get("resolved_principal")


def _review_signal_payload(raw_payload: dict[str, Any], content: str) -> dict[str, Any] | None:
    """Formato FLAT que `WorkItemLifecycleWorkflow.review_comment` lê.
    Retorna None quando o comentário de review não carrega veredito formal
    (não é uma decisão) — mesma regra da Fase 1."""
    review_state = str(raw_payload.get("source_ref", {}).get("review_state", "")).lower()
    if review_state in _CHANGES_REQUESTED_STATES:
        return {"verdict": "changes_requested", "comment": content}
    if review_state == "approved":
        return {"verdict": "approved", "comment": content}
    return None


def _plan_approval_payload(raw_payload: dict[str, Any], content: str) -> dict[str, Any]:
    """Payload do gate de aprovação de plano (SIGNAL_PLAN_APPROVAL), formato
    documentado em `dse_contracts.constants`: {verdict, route (obrigatório
    quando rejected), comment, actor}. `verdict`/`route` vêm de marcadores
    DETERMINÍSTICOS postos pelo adapter (`extra_payload` — ex.: valor do botão
    Slack "approve"/"reject" ou a coluna Jira de destino), default `approved`
    quando o adapter não os informou (WSA-E6-T3)."""
    verdict = str(raw_payload.get("approval_verdict", "approved")).lower()
    if verdict not in ("approved", "rejected"):
        verdict = "approved"
    p: dict[str, Any] = {"verdict": verdict, "comment": content, "actor": _actor_principal(raw_payload)}
    if verdict == "rejected":
        # WSB-E3-T3: route é obrigatório quando rejected.
        p["route"] = raw_payload.get("approval_route") or "re_plan"
    return p


def _route_signal(status: str | None, kind: str, raw_payload: dict[str, Any]) -> SignalRoute:
    """WSA-E6-T3 — roteamento de signal por STATUS do WorkItem (não mais só
    pelo `kind`, como na heurística da Fase 1). Determinístico (P1): nenhuma
    decisão de fluxo por LLM — só consulta `work_items.status` + marcadores
    postos pelo adapter.

    Preserva o comportamento da Fase 1 para `clarification_answer` e
    `review_comment`. O que muda é o `kind=approval`: passa a depender do
    status (gate de plano vs. review), e um webhook de merge (marcador
    `merged_by_human`) dispara `SIGNAL_MERGED_BY_HUMAN` (WSA-E4-T3)."""
    content = _content_of(raw_payload)

    # WSA-E4-T3: webhook pull_request merged -> merged_by_human. Marcador
    # determinístico posto pelo adapter-github; independe do status (o merge é
    # o terceiro pause point, o workflow só o consome quando está esperando).
    if raw_payload.get("merged_by_human"):
        return SignalRoute(
            SIGNAL_MERGED_BY_HUMAN,
            {"merged_by": raw_payload.get("merged_by"), "pr_number": raw_payload.get("pr_number")},
            "merged_by_human",
        )

    if kind == "clarification_answer":  # PRESERVA Fase 1
        return SignalRoute(SIGNAL_CLARIFICATION_ANSWER, {"text": content, "acceptance_criteria": content}, "clarification")

    if kind == "review_comment":  # PRESERVA Fase 1
        payload = _review_signal_payload(raw_payload, content)
        if payload is None:
            return SignalRoute(None, None, "review_comment_no_verdict")
        return SignalRoute(SIGNAL_REVIEW_COMMENT, payload, "review_comment")

    if kind == "steering":  # PRESERVA Fase 1 (best-effort, tratado como review)
        return SignalRoute(SIGNAL_REVIEW_COMMENT, {"text": content, "comment": content}, "steering")

    if kind == "approval":  # WSA-E6-T3: roteia por status
        if status == _STATUS_AWAITING_PLAN_APPROVAL:
            return SignalRoute(SIGNAL_PLAN_APPROVAL, _plan_approval_payload(raw_payload, content), "plan_approval")
        if status in _APPROVAL_REVIEW_STATES:
            return SignalRoute(SIGNAL_REVIEW_COMMENT, {"verdict": "approved", "comment": content}, "approval_as_review")
        # status inesperado -> NÃO adivinha (P6). Consumido com audit row.
        return SignalRoute(None, None, "unexpected_status")

    return SignalRoute(None, None, "unhandled_kind")


async def _dispatch_row(
    client: Client, *, work_item_id: str, event_id: str, kind: str, status: str | None, payload: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Retorna `(outcome, extra_details)`. `extra_details` alimenta a audit
    row emitida por `drain_once` (inclui o signal roteado e o motivo)."""
    if kind == _TASK_REQUEST_KIND:
        try:
            await client.start_workflow(
                WORKFLOW_TYPE,
                work_item_id,
                id=work_item_id,
                task_queue=TASK_QUEUE,
            )
            return DispatchOutcome.STARTED, {}
        except WorkflowAlreadyStartedError:
            # Idempotente por design (WSA-E1-T3): reentrega do mesmo
            # ingest_event (ou corrida entre 2 dispatchers) nunca e'
            # re-lancada como erro.
            return DispatchOutcome.DEDUPED_ALREADY_STARTED, {}

    route = _route_signal(status, kind, payload)

    if route.signal_name is None:
        if route.reason == "unexpected_status":
            # P6 decline-never-truncate: fronteira limpa, deixa evidência.
            return DispatchOutcome.DECLINED_UNEXPECTED_STATUS, {"status": status, "reason": route.reason}
        return DispatchOutcome.IGNORED_NOT_A_DECISION, {"reason": route.reason}

    handle = client.get_workflow_handle(work_item_id)
    try:
        await handle.signal(route.signal_name, route.payload)
        return DispatchOutcome.SIGNALED, {"signal": route.signal_name, "reason": route.reason, "status": status}
    except Exception:
        logger.exception(
            "signal_workflow falhou para work_item_id=%s event_id=%s (será retentado)",
            work_item_id, event_id,
        )
        return DispatchOutcome.SIGNAL_FAILED, {"signal": route.signal_name}


class Dispatcher:
    """Uso: `await Dispatcher(client).drain_once()` — drena um lote (até
    `batch_size` linhas) numa única transação `FOR UPDATE SKIP LOCKED`.
    Rode em loop (`run_forever`) como processo/worker separado do adapter."""

    def __init__(self, client: Client, *, batch_size: int = 25, conn_factory=get_connection):
        self._client = client
        self._batch_size = batch_size
        self._conn_factory = conn_factory

    async def drain_once(self) -> int:
        conn = self._conn_factory()
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT ie.id, ie.work_item_id, ie.event_id, ie.kind, ie.payload, wi.tenant_id, wi.status
                    FROM ingest_events ie
                    JOIN work_items wi ON wi.id = ie.work_item_id
                    WHERE NOT ie.processed
                    ORDER BY ie.id
                    FOR UPDATE OF ie SKIP LOCKED
                    LIMIT %s
                    """,
                    (self._batch_size,),
                )
                rows = cur.fetchall()

            if not rows:
                conn.rollback()
                return 0

            processed = 0
            for ingest_event_id, work_item_id, event_id, kind, payload, tenant_id, status in rows:
                outcome, extra_details = await _dispatch_row(
                    self._client,
                    work_item_id=work_item_id,
                    event_id=event_id,
                    kind=kind,
                    status=status,
                    payload=payload,
                )

                if outcome == DispatchOutcome.SIGNAL_FAILED:
                    # Não marca processed — próxima drenagem tenta de novo.
                    # (Sem backoff/dead-letter em Fase 1 — ver README.)
                    continue

                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE ingest_events SET processed = true, processed_at = now() WHERE id = %s",
                        (ingest_event_id,),
                    )

                audit_emit(
                    actor="system:ingest-gateway-dispatcher",
                    action=f"dispatch_{outcome}",
                    tenant_id=tenant_id,
                    work_item_id=work_item_id,
                    details={"event_id": event_id, "kind": kind, "ingest_event_id": ingest_event_id, **extra_details},
                    conn=conn,
                )
                processed += 1

            conn.commit()
            return processed
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    async def drain_all(self, max_rounds: int = 1000) -> int:
        """Drena até esvaziar a fila (ou `max_rounds` iterações, guarda
        contra loop infinito se algo estiver reintroduzindo trabalho).

        Achado da integração da Fase 1: parar no primeiro round vazio é
        seguro em produção (`run_forever` chama `drain_once` para sempre, um
        round vazio transitório só atrasa — nunca perde nada, ver
        `dispatcher_main.py`), mas é instável para o uso "drene tudo agora e
        me diga que terminou" deste método sob concorrência real: com 2+
        dispatchers, um round pode ver 0 linhas só porque o outro dispatcher
        está segurando o lock das últimas linhas disponíveis naquele
        instante (SKIP LOCKED as pula, não as conta como inexistentes) — não
        significa fila vazia. Exigir 2 rounds vazios CONSECUTIVOS reduz essa
        janela de corrida a um nível desprezível sem reintroduzir espera
        ilimitada (P6 decline-never-truncate continua valendo: `max_rounds`
        ainda é o teto duro). Risco residual documentado com honestidade:
        isto é uma heurística, não uma garantia formal — em produção o
        dispatcher roda via `run_forever` (loop contínuo, nunca chama
        `drain_all`), então uma lacuna transitória aqui nunca é perda
        permanente, só atraso até o próximo round do processo real."""
        total = 0
        consecutive_empty = 0
        for _ in range(max_rounds):
            n = await self.drain_once()
            total += n
            if n == 0:
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    break
                await asyncio.sleep(0.05 * consecutive_empty)
            else:
                consecutive_empty = 0
        return total

    async def run_forever(self, poll_interval_seconds: float = 1.0) -> None:  # pragma: no cover - loop de produção
        import asyncio

        while True:
            n = await self.drain_once()
            if n == 0:
                await asyncio.sleep(poll_interval_seconds)
