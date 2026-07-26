"""Call-time enforcement (WSD-E2 + WSD-E4-T2) — the single point where policy,
budget, kill switch and reassign apply to EVERY model call.

`enforce_call(headers, requested_model)` is called by
`gateway_call.chat_completion` BEFORE any HTTP to LiteLLM. Order:

  1. in-flight model reassign (WSD-E4-T2) — swaps the effective model;
  2. kill switch (WSD-E4-T2) — global/tenant/work_item -> refuse;
  3. policy (WSD-E2-T1) — the effective model must be in the scope's allowlist;
  4. budget (WSD-E2-T2) — work_item and tenant spent-so-far vs caps.

Every refusal:
  - is a clean BOUNDARY (P6 decline-never-truncate): raises
    `GatewayCallError(status, body)` with `body` in
    `dse_contracts.gateway_contract.GatewayErrorResponse` format
    (`error in {policy_denied, budget_exhausted}`), which the WS-B workflow
    turns into Failed;
  - produces an audit row (P8) via `dse_audit.emit`;
  - is NOT decided by an LLM (P1) — it is deterministic code reading
    config/state.

Enforcement is PERMISSIVE by default: with no policy configured, no cap, no
kill switch and no reassign, `enforce_call` returns the requested model
unchanged — Phase 1 behavior is preserved for tenants with no config.

Architecture note (honest): this enforcement runs on the gateway's CLIENT path.
The server-side backstop already exists and is NOT bypassable by the sandbox:
virtual keys are scoped per model in LiteLLM (native 403) and can carry
`max_budget`/`duration`. For a 100% non-bypassable deployment, this same
`enforce_call` must be mirrored as a LiteLLM proxy pre-call hook (custom
callback) — see README §"What is still missing for production".
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
    """Builds the refusal body (validatable as GatewayErrorResponse), emits
    audit and returns the exception ready for the caller to raise."""
    body = GatewayErrorResponse(error=error, message=message, retryable=False).model_dump()
    body.update(extra)  # extra detail (kind/scope/cap) — extras ignored on validation
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

    # 1. in-flight reassign (operator) — the effective model may change.
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

    # 3. policy — the effective model must be in the scope's allowlist.
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
