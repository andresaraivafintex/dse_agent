"""Consumption contract of the model-gateway (WSD-E1-T4). Every agent session
(Coder, and later Planner/Tester/Reviewer) calls the gateway exclusively
through this interface — never a provider SDK directly (the WS-C egress proxy
would block it even if it tried; this documents the happy contract).
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class Stage(str, Enum):
    coder = "coder"
    planner = "planner"  # Phase 2
    tester = "tester"  # Phase 2
    reviewer = "reviewer"  # Phase 2


class GatewayCallHeaders(BaseModel):
    """Headers required on every gateway call — they enable policy/budget
    enforcement at call time (FR-12) without the agent knowing about either."""

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
    """Shape of a policy/budget refusal — Temporal treats this as a boundary
    (P6 decline-never-truncate), never as output truncation."""

    error: str  # "policy_denied" | "budget_exhausted" | "model_unavailable"
    message: str
    retryable: bool = False
