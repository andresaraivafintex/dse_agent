"""Endpoint mínimo de export de custo (WSD-E3-T2). Um FastAPI standalone
opcional — o cliente Python (`cost_export.aggregate_cost`) é a peça central
e reutilizável; isto é só uma casca HTTP fina em cima dela para permitir
`curl` manual/dashboards simples sem escrever Python.

Não faz parte do caminho de chamada de modelo (não é consumido pelo Coder) —
é observabilidade/reporting, then roda como processo separado se alguém
quiser (`uvicorn model_gateway_client.export_api:app`). Produção: isto vira
uma query no backend do collector do WS-F (ver cost_export.py).
"""
from __future__ import annotations

from fastapi import FastAPI

from .cost_export import aggregate_cost, aggregate_cost_by_tenant

app = FastAPI(title="dse-model-gateway-cost-export")


@app.get("/internal/cost-export")
def cost_export(tenant_id: str | None = None) -> list[dict]:
    return aggregate_cost(tenant_id=tenant_id)


@app.get("/internal/cost-export/by-tenant")
def cost_export_by_tenant() -> dict[str, float]:
    return aggregate_cost_by_tenant()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
