"""WSE-E6-T15 — resume do workflow por comentário de review (núcleo do UC4).

Recebe um `ConversationEvent` já correlacionado e verificado pelo WS-A + o
`work_item_id` resolvido (a correlação "qual work_item_id esse comentário/PR
pertence" é responsabilidade do WS-A — ver README §Cross-workstream), traduz
o CONTEÚDO em uma decisão humana (`approved` | `changes_requested`) e
sinaliza o workflow via `signal_workflow` usando o MESMO workflow_id
(=work_item_id) que o WS-B espera (WSB-E3-T4).

P1 (deterministic-or-human): a interpretação usa só campos estruturados que o
WS-A já normalizou (`EventKind.approval`, ou `source_ref["review_state"]`
para reviews formais do GitHub) — nunca um LLM. Um `review_comment` "solto"
(sem estado formal de aprovação/mudança) não é uma decisão: não sinaliza
nada, não cria WorkItem, não cria PR — só retorna `False`.
"""
from __future__ import annotations

from typing import Literal, Protocol

from dse_contracts import ConversationEvent, EventKind
from dse_contracts.constants import SIGNAL_REVIEW_COMMENT

try:
    from dse_audit import emit as audit_emit
except ImportError:  # pragma: no cover
    audit_emit = None

# Achado da integração da Fase 1: este módulo usava sua própria constante
# local ("review_decision"), que não batia com o `@workflow.signal def
# review_comment(...)` real do WS-B — o signal nunca chegava ao handler.
# Corrigido para importar o nome canônico de dse_contracts.constants (mesma
# constante que o dispatcher do WS-A agora usa para kind=review_comment).
REVIEW_DECISION_SIGNAL_NAME = SIGNAL_REVIEW_COMMENT

ReviewDecision = Literal["approved", "changes_requested"]

_CHANGES_REQUESTED_STATES = {"changes_requested", "request_changes", "changes-requested"}
_APPROVED_STATES = {"approved"}


class WorkflowHandle(Protocol):
    async def signal(self, name: str, arg: object | None = None) -> None: ...


class TemporalClientLike(Protocol):
    def get_workflow_handle(self, workflow_id: str) -> WorkflowHandle: ...


def interpret_review_decision(event: ConversationEvent) -> ReviewDecision | None:
    """Determinístico, sem LLM. `EventKind.approval` sempre vira "approved".
    `EventKind.review_comment` só vira uma decisão se o WS-A já anexou um
    `review_state` explícito em `source_ref` (estado formal de review do
    GitHub: APPROVED/CHANGES_REQUESTED) — um comentário de review comum
    (`review_state` ausente ou "commented") não é uma decisão."""
    if event.kind == EventKind.approval:
        return "approved"
    if event.kind == EventKind.review_comment:
        review_state = str(event.source_ref.get("review_state", "")).lower()
        if review_state in _CHANGES_REQUESTED_STATES:
            return "changes_requested"
        if review_state in _APPROVED_STATES:
            return "approved"
        return None
    return None


async def handle_review_event(
    event: ConversationEvent,
    work_item_id: str,
    tenant_id: str,
    temporal_client: TemporalClientLike,
    actor: str | None = None,
) -> bool:
    """Retorna True se sinalizou o workflow; False se o evento não carregava
    uma decisão de review — nesse caso NENHUM efeito colateral acontece
    (nem WorkItem novo, nem PR novo, nem signal)."""
    decision = interpret_review_decision(event)
    if decision is None:
        return False

    handle = temporal_client.get_workflow_handle(work_item_id)
    resolved_actor = actor or event.actor.resolved_principal or f"platform_user:{event.actor.platform_user_id}"
    # Achado da integração da Fase 1: o workflow (services/orchestrator/src/
    # dse_orchestrator/workflows.py) lê `payload["verdict"]` e
    # `payload["comment"]` — este módulo enviava `"decision"` (chave
    # diferente) e nenhum `"comment"`, então o veredito nunca era lido
    # corretamente (caía no ramo "unknown_review_verdict" e escalava).
    # Corrigido para o formato real consumido pelo workflow.
    payload = {
        "verdict": decision,
        "comment": event.content_snapshot,
        "event_id": event.event_id,
        "actor": resolved_actor,
    }
    await handle.signal(REVIEW_DECISION_SIGNAL_NAME, payload)

    if audit_emit is not None:
        audit_emit(
            actor=resolved_actor,
            action="review_decision_signaled",
            tenant_id=tenant_id,
            work_item_id=work_item_id,
            details=payload,
        )
    return True
