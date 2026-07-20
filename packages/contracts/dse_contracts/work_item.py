"""WorkItem (control plane, §10.3) e a API pública WSA-E1-T4 (`DseTaskRequest`/
`DseTaskStatus`) — projeção deliberadamente mais grossa do estado interno.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class WorkItemStatus(str, Enum):
    """Máquina de estados §9.3."""

    new = "new"
    needs_clarification = "needs_clarification"
    ready = "ready"
    queued = "queued"
    # Fase 2 (WSB-E3-T2): gate de aprovação de plano por risk class. Estado
    # durável em que o WorkItem estaciona esperando um humano aprovar o plano
    # de alto risco (o WS-A já roteava `SIGNAL_PLAN_APPROVAL` com base nele —
    # a constante em constants.py referenciava este estado antes dele existir
    # no enum; gap de fundação corrigido na integração da Fase 2).
    awaiting_plan_approval = "awaiting_plan_approval"
    implementing = "implementing"
    validating = "validating"
    pr_ready = "pr_ready"
    review_feedback = "review_feedback"
    done = "done"
    blocked = "blocked"
    failed = "failed"
    escalated = "escalated"


# Mapa único do estado interno -> estado público grosseiro (WSA-E1-T4).
# Adicionar um estado interno novo aqui é obrigatório (o teste de contrato falha
# se algum WorkItemStatus não estiver no mapa) — nunca deixe implícito.
PublicStatus = Literal["running", "blocked", "done", "failed"]

_PUBLIC_STATUS_MAP: dict[WorkItemStatus, PublicStatus] = {
    WorkItemStatus.new: "running",
    WorkItemStatus.needs_clarification: "blocked",
    WorkItemStatus.ready: "running",
    WorkItemStatus.queued: "running",
    # awaiting_plan_approval -> "blocked" (§10.3: "AwaitingPlanApproval /
    # Escalated -> blocked com razão no detalhe do WorkItem").
    WorkItemStatus.awaiting_plan_approval: "blocked",
    WorkItemStatus.implementing: "running",
    WorkItemStatus.validating: "running",
    WorkItemStatus.pr_ready: "running",
    WorkItemStatus.review_feedback: "running",
    WorkItemStatus.done: "done",
    WorkItemStatus.blocked: "blocked",
    WorkItemStatus.failed: "failed",
    WorkItemStatus.escalated: "blocked",
}


def to_public_status(status: WorkItemStatus) -> PublicStatus:
    try:
        return _PUBLIC_STATUS_MAP[status]
    except KeyError as e:  # pragma: no cover - defensive, covered by contract test
        raise ValueError(f"WorkItemStatus {status} sem projeção pública definida") from e


class WorkItem(BaseModel):
    id: str  # dobra como Temporal workflow_id
    tenant_id: str
    source: Literal["slack", "github", "jira"]
    source_ref: dict[str, Any]
    repo: str | None = None
    base_branch: str | None = None
    requester: str  # principal resolvido
    data_class: str = "internal"
    status: WorkItemStatus = WorkItemStatus.new
    risk_class: str | None = None
    plan: dict[str, Any] | None = None
    pr_number: int | None = None
    budget: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


class DseTaskRequest(BaseModel):
    """Tipo público de intake — o que um consumidor externo vê ao pedir uma tarefa."""

    tenant_id: str
    source: Literal["slack", "github", "jira"]
    repo: str | None = None
    requester: str
    idempotency_key: str
    content_snapshot: str


class DseTaskStatus(BaseModel):
    """Tipo público de status — consumido por admin queue board (Fase 2) e adapters."""

    work_item_id: str
    status: PublicStatus
    detail: str | None = None  # razão, presente quando status == blocked/failed
    pr_number: int | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_work_item(cls, wi: WorkItem, detail: str | None = None) -> "DseTaskStatus":
        return cls(
            work_item_id=wi.id,
            status=to_public_status(wi.status),
            detail=detail,
            pr_number=wi.pr_number,
            updated_at=wi.updated_at,
        )
