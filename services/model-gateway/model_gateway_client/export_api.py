"""Minimal cost export endpoint (WSD-E3-T2). An optional standalone FastAPI —
the Python client (`cost_export.aggregate_cost`) is the central, reusable
piece; this is just a thin HTTP shell on top of it so manual `curl` / simple
dashboards work without writing Python.

Not part of the model call path (the Coder does not consume it) — it is
observability/reporting, and runs as a separate process if anyone wants it
(`uvicorn model_gateway_client.export_api:app`). In production this becomes a
query against the WS-F collector backend (see cost_export.py).
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
