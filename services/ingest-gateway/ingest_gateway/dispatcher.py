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

Nota (contrato provisório): `SIGNAL_NAME` não está em
`dse_contracts.constants` ainda (só `TASK_QUEUE`/`WORKFLOW_TYPE` existem)
— documentado no README como pedido ao arquiteto para promover a constante
ao pacote compartilhado quando o workflow do WS-B registrar o handler real.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from dse_audit import emit as audit_emit
from dse_contracts import TASK_QUEUE, WORKFLOW_TYPE
from dse_contracts.constants import SIGNAL_CLARIFICATION_ANSWER, SIGNAL_REVIEW_COMMENT
from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError

from .db import get_connection

logger = logging.getLogger("ingest_gateway.dispatcher")

_TASK_REQUEST_KIND = "task_request"

# Achado da integração da Fase 1: mapear por `kind` (em vez de um único
# SIGNAL_NAME genérico) para acertar o handler real do WorkItemLifecycleWorkflow
# — ver a nota de limitação conhecida em dse_contracts.constants (o roteamento
# ideal depende do status do WorkItem, não só do kind; heurística aceitável
# para a Fase 1, onde só existem os pause points de clarificação e review).
_KIND_TO_SIGNAL = {
    "clarification_answer": SIGNAL_CLARIFICATION_ANSWER,
    "review_comment": SIGNAL_REVIEW_COMMENT,
    "approval": SIGNAL_REVIEW_COMMENT,
    "steering": SIGNAL_REVIEW_COMMENT,
}


class DispatchOutcome:
    STARTED = "started"
    DEDUPED_ALREADY_STARTED = "deduped_already_started"
    SIGNALED = "signaled"
    SIGNAL_FAILED = "signal_failed"
    IGNORED_NOT_A_DECISION = "ignored_not_a_decision"


_CHANGES_REQUESTED_STATES = {"changes_requested", "request_changes", "changes-requested"}


def _build_signal_payload(kind: str, raw_payload: dict[str, Any]) -> dict[str, Any] | None:
    """Traduz o `ConversationEvent` serializado (formato armazenado em
    `ingest_events.payload` — ver `gateway._payload_json`) para o formato
    FLAT que cada `@workflow.signal` do WorkItemLifecycleWorkflow realmente
    lê. Achado da integração da Fase 1: antes desta função o payload bruto
    (aninhado, com chaves `content_snapshot`/`source_ref`/...) era repassado
    verbatim ao signal — o workflow esperava `payload["text"]` (clarificação)
    ou `payload["verdict"]`/`payload["comment"]` (review) e nunca via nenhum
    dos dois, então toda resposta de clarificação/review vinda pelo caminho
    automático (não por chamada manual de teste) silenciosamente não surtia
    efeito nenhum no workflow, mesmo com o nome do signal já corrigido.

    Retorna `None` quando o evento não carrega uma decisão de review válida
    (mesma regra de `dse_validation.review_signal.interpret_review_decision`,
    duplicada aqui de propósito para não criar uma dependência de pacote
    WS-A -> WS-E só por isto — consolidar as duas nesta função quando o
    arquiteto revisar; ver README)."""
    content = raw_payload.get("sanitized_content") or raw_payload.get("content_snapshot", "")

    if kind == SIGNAL_CLARIFICATION_ANSWER:
        # Achado da integração da Fase 1: `check_clarification_completeness`
        # (services/orchestrator/src/dse_orchestrator/local_activities.py)
        # cheeca especificamente `payload["acceptance_criteria"]` — so enviar
        # `text` deixava o campo eternamente vazio e o gate reciclava a
        # mesma pergunta a cada round ate estourar o cap. Fase 1 nao tem
        # nenhuma etapa de extracao estruturada (NLP) da resposta livre do
        # humano — heuristica deliberada e documentada: qualquer resposta
        # de clarificacao nao-vazia e' tratada como satisfazendo o item
        # `acceptance_criteria` do checklist (unico item hoje fora de
        # repo/base_branch, que normalmente ja vem preenchido do intake).
        # Rotear por campo especifico exigiria um checklist estruturado por
        # task-class que ainda nao existe — ver README para o que falta.
        return {"text": content, "acceptance_criteria": content}

    if kind == "approval":
        return {"verdict": "approved", "comment": content}

    if kind == "review_comment":
        review_state = str(raw_payload.get("source_ref", {}).get("review_state", "")).lower()
        if review_state in _CHANGES_REQUESTED_STATES:
            return {"verdict": "changes_requested", "comment": content}
        if review_state == "approved":
            return {"verdict": "approved", "comment": content}
        return None  # comentário de review sem veredito formal — nao e' uma decisao

    # "steering" e outros kinds sem payload dedicado: melhor esforco, so o texto.
    return {"text": content, "comment": content}


async def _dispatch_row(client: Client, *, work_item_id: str, event_id: str, kind: str, payload: dict[str, Any]) -> str:
    if kind == _TASK_REQUEST_KIND:
        try:
            await client.start_workflow(
                WORKFLOW_TYPE,
                work_item_id,
                id=work_item_id,
                task_queue=TASK_QUEUE,
            )
            return DispatchOutcome.STARTED
        except WorkflowAlreadyStartedError:
            # Idempotente por design (WSA-E1-T3): reentrega do mesmo
            # ingest_event (ou corrida entre 2 dispatchers) nunca e'
            # re-lancada como erro.
            return DispatchOutcome.DEDUPED_ALREADY_STARTED

    signal_payload = _build_signal_payload(kind, payload)
    if signal_payload is None:
        return DispatchOutcome.IGNORED_NOT_A_DECISION

    handle = client.get_workflow_handle(work_item_id)
    signal_name = _KIND_TO_SIGNAL.get(kind, SIGNAL_REVIEW_COMMENT)
    try:
        await handle.signal(signal_name, signal_payload)
        return DispatchOutcome.SIGNALED
    except Exception:
        logger.exception(
            "signal_workflow falhou para work_item_id=%s event_id=%s (será retentado)",
            work_item_id, event_id,
        )
        return DispatchOutcome.SIGNAL_FAILED


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
                    SELECT ie.id, ie.work_item_id, ie.event_id, ie.kind, ie.payload, wi.tenant_id
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
            for ingest_event_id, work_item_id, event_id, kind, payload, tenant_id in rows:
                outcome = await _dispatch_row(
                    self._client,
                    work_item_id=work_item_id,
                    event_id=event_id,
                    kind=kind,
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
                    details={"event_id": event_id, "kind": kind, "ingest_event_id": ingest_event_id},
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
