"""API de controle de operador do gateway (WSD-E4-T2). FastAPI fina sobre
`controls.py` / `budget.py` / `policy.py` — é o "signal/endpoint" que os
controles de operador do WS-B/WS-F chamam para acionar kill switch, reassignar
modelo de uma tarefa em voo, e inspecionar budget/política.

NÃO faz parte do caminho de chamada de modelo (não é o Coder que chama isto) —
é plano de controle. Roda como processo separado:

    uvicorn model_gateway_client.control_api:app --port 4010

Toda mutação já emite audit (P8) via as funções de `controls.py`. Autenticação
do endpoint em si é responsabilidade do WS-F (mesh/mTLS/authz do admin console)
— documentado no README; aqui expomos a superfície funcional.
"""
from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel

from . import budget as budget_mod
from . import controls, policy

app = FastAPI(title="dse-model-gateway-control")


class KillSwitchRequest(BaseModel):
    scope_type: str  # global | tenant | work_item | channel
    scope_id: str = "*"
    enabled: bool = True
    reason: str | None = None
    actor: str = "system:operator"
    tenant_id: str | None = None


class ReassignRequest(BaseModel):
    work_item_id: str
    to_model: str
    reason: str | None = None
    actor: str = "system:operator"
    tenant_id: str = "*"


@app.post("/internal/kill-switch")
def set_kill_switch(req: KillSwitchRequest) -> dict:
    controls.set_kill_switch(
        req.scope_type,
        req.scope_id,
        enabled=req.enabled,
        reason=req.reason,
        actor=req.actor,
        tenant_id=req.tenant_id,
    )
    return {"ok": True, "scope_type": req.scope_type, "scope_id": req.scope_id, "enabled": req.enabled}


@app.get("/internal/kill-switch/check")
def check_kill_switch(tenant_id: str, work_item_id: str) -> dict:
    decision = controls.is_killed(tenant_id, work_item_id)
    if decision is None:
        return {"killed": False}
    return {
        "killed": True,
        "scope_type": decision.scope_type,
        "scope_id": decision.scope_id,
        "reason": decision.reason,
        "source": decision.source,
    }


@app.post("/internal/reassign-model")
def reassign_model(req: ReassignRequest) -> dict:
    controls.reassign_model(
        req.work_item_id,
        req.to_model,
        reason=req.reason,
        actor=req.actor,
        tenant_id=req.tenant_id,
    )
    return {"ok": True, "work_item_id": req.work_item_id, "to_model": req.to_model}


@app.delete("/internal/reassign-model/{work_item_id}")
def clear_reassignment(work_item_id: str, actor: str = "system:operator") -> dict:
    controls.clear_reassignment(work_item_id, actor=actor)
    return {"ok": True, "work_item_id": work_item_id}


@app.get("/internal/budget-status")
def budget_status(tenant_id: str, work_item_id: str) -> dict:
    st = budget_mod.get_status(tenant_id, work_item_id)
    return {
        "tenant_id": tenant_id,
        "work_item_id": work_item_id,
        "work_item_spent_usd": round(st.work_item_spent_usd, 6),
        "work_item_cap_usd": st.caps.work_item_cap_usd,
        "work_item_cap_source": st.caps.work_item_source,
        "work_item_exhausted": st.work_item_exhausted,
        "tenant_spent_usd": round(st.tenant_spent_usd, 6),
        "tenant_monthly_cap_usd": st.caps.tenant_monthly_cap_usd,
        "tenant_cap_source": st.caps.tenant_source,
        "tenant_exhausted": st.tenant_exhausted,
    }


@app.get("/internal/policy")
def resolve_policy(
    tenant_id: str, stage: str, data_class: str = "internal", risk_class: str = "*"
) -> dict:
    decision = policy.resolve_policy(
        tenant_id, stage, data_class=data_class, risk_class=risk_class
    )
    return {
        "allowed_models": decision.allowed_models,
        "preferred_model": decision.preferred_model,
        "source": decision.source,
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
