"""Enforcement no call time (WSD-E2 + WSD-E4-T2) — o ponto único onde política,
budget, kill switch e reassign se aplicam a CADA chamada de modelo.

`enforce_call(headers, requested_model)` é chamado por
`gateway_call.chat_completion` ANTES de qualquer HTTP para o LiteLLM. Ordem:

  1. reassign de modelo em voo (WSD-E4-T2) — troca o modelo efetivo;
  2. kill switch (WSD-E4-T2) — global/tenant/work_item -> recusa;
  3. política (WSD-E2-T1) — modelo efetivo tem que estar no allowlist do escopo;
  4. budget (WSD-E2-T2) — spent-so-far do work_item e do tenant vs caps.

Toda recusa:
  - é uma FRONTEIRA limpa (P6 decline-never-truncate): levanta
    `GatewayCallError(status, body)` com `body` no formato
    `dse_contracts.gateway_contract.GatewayErrorResponse`
    (`error in {policy_denied, budget_exhausted}`), que o workflow do WS-B
    converte em Failed;
  - gera uma linha de audit (P8) via `dse_audit.emit`;
  - NÃO é decidida por um LLM (P1) — é código determinístico lendo config/estado.

Enforcement é PERMISSIVO por default: sem política configurada, sem cap, sem
kill switch e sem reassign, `enforce_call` devolve o modelo requisitado
inalterado — o comportamento da Fase 1 é preservado para tenants sem config.

Nota de arquitetura (honesta): este enforcement roda no caminho do CLIENTE do
gateway. O backstop server-side já existe e NÃO é burlável pelo sandbox: as
virtual keys são escopadas por modelo no LiteLLM (403 nativo) e podem levar
`max_budget`/`duration`. Para um deployment 100% non-bypassable, o mesmo
`enforce_call` deve ser espelhado como um pre-call hook do LiteLLM proxy
(callback custom) — ver README §"O que falta para produção".
"""
from __future__ import annotations

from dataclasses import dataclass

from dse_audit.client import emit as audit_emit
from dse_contracts.gateway_contract import GatewayCallHeaders, GatewayErrorResponse

from . import budget, controls, policy
from .errors import GatewayCallError

_AUDIT_ACTOR = "system:model-gateway"


@dataclass(frozen=True)
class EnforcementResult:
    effective_model: str
    reassigned_from: str | None
    policy_source: str


def _deny(
    *,
    error: str,
    message: str,
    status_code: int,
    headers: GatewayCallHeaders,
    action: str,
    extra: dict,
) -> "GatewayCallError":
    """Constrói o corpo de recusa (validável como GatewayErrorResponse), emite
    audit e retorna a exceção pronta para o caller levantar."""
    body = GatewayErrorResponse(error=error, message=message, retryable=False).model_dump()
    body.update(extra)  # detalhe adicional (kind/scope/cap) — extras ignorados na validação
    audit_emit(
        actor=_AUDIT_ACTOR,
        action=action,
        tenant_id=headers.tenant_id,
        work_item_id=headers.work_item_id,
        details={
            "stage": headers.stage.value,
            "task_class": headers.task_class,
            "data_class": headers.data_class,
            "error": error,
            "message": message,
            **extra,
        },
    )
    return GatewayCallError(status_code, body)


def enforce_call(headers: GatewayCallHeaders, requested_model: str) -> EnforcementResult:
    tenant_id = headers.tenant_id
    work_item_id = headers.work_item_id

    # 1. reassign em voo (operador) — modelo efetivo pode mudar.
    reassigned_to = controls.resolve_reassignment(work_item_id)
    effective_model = reassigned_to or requested_model
    reassigned_from = requested_model if reassigned_to else None

    # 2. kill switch (global/tenant/work_item).
    kill = controls.is_killed(tenant_id, work_item_id)
    if kill is not None:
        raise _deny(
            error="policy_denied",
            message=f"kill switch active (scope={kill.scope_type}:{kill.scope_id})",
            status_code=403,
            headers=headers,
            action="gateway.call_denied_kill_switch",
            extra={
                "kind": "kill_switch",
                "scope_type": kill.scope_type,
                "scope_id": kill.scope_id,
                "reason": kill.reason,
                "source": kill.source,
            },
        )

    # 3. política — modelo efetivo tem que estar no allowlist do escopo.
    decision = policy.resolve_policy(
        tenant_id,
        headers.stage.value,
        data_class=headers.data_class,
        risk_class="*",
    )
    if not decision.permits(effective_model):
        raise _deny(
            error="policy_denied",
            message=(
                f"model '{effective_model}' not permitted for "
                f"stage={headers.stage.value} tenant={tenant_id}"
            ),
            status_code=403,
            headers=headers,
            action="gateway.call_denied_policy",
            extra={
                "kind": "model_not_allowed",
                "requested_model": requested_model,
                "effective_model": effective_model,
                "allowed_models": decision.allowed_models,
                "policy_source": decision.source,
            },
        )

    # 4. budget — spent-so-far vs caps (work_item + tenant).
    status = budget.get_status(tenant_id, work_item_id)
    if status.work_item_exhausted:
        raise _deny(
            error="budget_exhausted",
            message=(
                f"work_item budget exhausted: spent "
                f"${status.work_item_spent_usd:.4f} >= cap ${status.caps.work_item_cap_usd:.4f}"
            ),
            status_code=402,
            headers=headers,
            action="gateway.call_denied_budget",
            extra={
                "kind": "work_item",
                "spent_usd": round(status.work_item_spent_usd, 6),
                "cap_usd": status.caps.work_item_cap_usd,
                "cap_source": status.caps.work_item_source,
            },
        )
    if status.tenant_exhausted:
        raise _deny(
            error="budget_exhausted",
            message=(
                f"tenant monthly budget exhausted: spent "
                f"${status.tenant_spent_usd:.4f} >= cap ${status.caps.tenant_monthly_cap_usd:.4f}"
            ),
            status_code=402,
            headers=headers,
            action="gateway.call_denied_budget",
            extra={
                "kind": "tenant",
                "spent_usd": round(status.tenant_spent_usd, 6),
                "cap_usd": status.caps.tenant_monthly_cap_usd,
                "cap_source": status.caps.tenant_source,
            },
        )

    return EnforcementResult(
        effective_model=effective_model,
        reassigned_from=reassigned_from,
        policy_source=decision.source,
    )
