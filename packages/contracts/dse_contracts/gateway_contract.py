"""Contrato de consumo do model-gateway (WSD-E1-T4). Toda sessão de agente
(Coder, e futuramente Planner/Tester/Reviewer) chama o gateway exclusivamente
por esta interface — nunca um SDK de provider diretamente (o egress proxy do
WS-C bloquearia mesmo que tentasse; isto documenta o contrato feliz).
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class Stage(str, Enum):
    coder = "coder"
    planner = "planner"  # Fase 2
    tester = "tester"  # Fase 2
    reviewer = "reviewer"  # Fase 2


class GatewayCallHeaders(BaseModel):
    """Headers obrigatórios em toda chamada ao gateway — permitem policy/budget
    enforcement no call time (FR-12) sem o agente saber de nenhum dos dois."""

    tenant_id: str
    work_item_id: str
    stage: Stage
    task_class: str = "default"
    data_class: str = "internal"

    def to_http_headers(self) -> dict[str, str]:
        return {
            "X-Dse-Tenant-Id": self.tenant_id,
            "X-Dse-Work-Item-Id": self.work_item_id,
            "X-Dse-Stage": self.stage.value,
            "X-Dse-Task-Class": self.task_class,
            "X-Dse-Data-Class": self.data_class,
        }


class GatewayErrorResponse(BaseModel):
    """Formato de recusa de política/budget — o Temporal trata isto como
    fronteira (P6 decline-never-truncate), nunca como truncamento de output."""

    error: str  # "policy_denied" | "budget_exhausted" | "model_unavailable"
    message: str
    retryable: bool = False
