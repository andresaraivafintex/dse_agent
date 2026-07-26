"""The single client-side model call path (WSD-E1-T4 / WSD-E3-T1). Every agent
(Coder, and later Planner/Tester/Reviewer) calls `chat_completion` — never a
provider SDK (`anthropic`, `boto3`) directly. That is what
`test_conformance_gateway_only.py` proves.

Uses `dse_contracts.gateway_contract.GatewayCallHeaders` (contract already
published by the foundation) for the headers required by call-time
policy/budget enforcement — but THIS module neither decides nor applies policy
(that is WSD-E2, Phase 2). It only propagates the headers and handles the
response.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from dse_audit.client import emit as _audit_emit
from dse_contracts.gateway_contract import GatewayCallHeaders, GatewayErrorResponse

from . import enforcement, failover, ledger, settings, telemetry
from .errors import GatewayCallError

# Header LiteLLM uses to report the real cost of the call (computed by it from
# model + tokens — never recomputed by us).
_COST_HEADER = "x-litellm-response-cost-original"


@dataclass(frozen=True)
class ChatCompletionResult:
    content: str
    model: str
    cost_usd: float
    tokens_in: int
    tokens_out: int
    raw: dict[str, Any]


def chat_completion(
    *,
    headers: GatewayCallHeaders,
    virtual_key: str,
    model: str,
    messages: list[dict[str, str]],
    timeout: float = 30.0,
    **extra_params: Any,
) -> ChatCompletionResult:
    """Calls `POST {gateway_base_url}/v1/chat/completions`. Never calls any
    other host — `settings.gateway_base_url()` is the only base URL used by
    this module (see the conformance test).

    BEFORE any HTTP, it applies call-time enforcement (WSD-E2/E4-T2): model
    reassign, kill switch, policy and budget. A denial raises `GatewayCallError`
    with a `GatewayErrorResponse` body (P6 decline-never-truncate) and has
    already emitted the audit row (P8) — the WS-B workflow turns that into
    Failed. AFTER a 2xx response, it records the real cost on the durable
    ledger (WSD-E3-T4).
    """
    # Enforcement boundary (may raise GatewayCallError + emit audit).
    enf = enforcement.enforce_call(headers, model)
    model = enf.effective_model  # honors in-flight model reassign

    url = f"{settings.gateway_base_url()}/v1/chat/completions"
    http_headers = {
        **headers.to_http_headers(),
        "Authorization": f"Bearer {virtual_key}",
        "Content-Type": "application/json",
    }
    body: dict[str, Any] = {"model": model, "messages": messages, **extra_params}

    with telemetry.model_call_span(
        tenant_id=headers.tenant_id,
        work_item_id=headers.work_item_id,
        stage=headers.stage.value,
        model=model,
        task_class=headers.task_class,
    ) as span:
        try:
            resp = httpx.post(url, json=body, headers=http_headers, timeout=timeout)
        except httpx.HTTPError as exc:
            span.record_exception(exc)
            span.set_status(trace_status_error())
            # P8: a transport failure (gateway unreachable / timeout) is
            # evidence too — same action as the upstream error, status_code=0.
            with _best_effort():
                _audit_emit(
                    actor="system:model-gateway",
                    action="gateway.call_failed_upstream",
                    tenant_id=headers.tenant_id,
                    work_item_id=headers.work_item_id,
                    details={
                        "stage": headers.stage.value,
                        "task_class": headers.task_class,
                        "model": model,
                        "status_code": 0,
                        "error_body": f"transport_error: {exc}",
                    },
                )
            raise GatewayCallError(0, {"error": "transport_error", "message": str(exc)}) from exc

        if resp.status_code >= 300:
            span.set_status(trace_status_error())
            error_body = _safe_json(resp)
            # P6 boundary: fail clean, never "carry on anyway". If the body
            # matches the published error contract, validate against it
            # (documents the expected shape; does not change behavior).
            with _best_effort():
                GatewayErrorResponse.model_validate(error_body)
            # WSD-E4-T3 (P8): a failure coming from upstream (full provider
            # outage, 429 quota, auth) also becomes evidence on the audit
            # ledger — never just an exception that gets lost in the log.
            # Wrapped in best-effort: an audit failure must not MASK the real
            # error.
            with _best_effort():
                _audit_emit(
                    actor="system:model-gateway",
                    action="gateway.call_failed_upstream",
                    tenant_id=headers.tenant_id,
                    work_item_id=headers.work_item_id,
                    details={
                        "stage": headers.stage.value,
                        "task_class": headers.task_class,
                        "model": model,
                        "status_code": resp.status_code,
                        "attempted_fallbacks": resp.headers.get(
                            failover.ATTEMPTED_FALLBACKS_HEADER
                        ),
                        "error_body": _truncate_for_audit(error_body),
                    },
                )
            raise GatewayCallError(resp.status_code, error_body)

        payload = resp.json()
        usage = payload.get("usage", {})
        tokens_in = int(usage.get("prompt_tokens", 0))
        tokens_out = int(usage.get("completion_tokens", 0))
        cost_usd = _extract_cost(resp)
        returned_model = payload.get("model", model)

        telemetry.set_usage_attributes(
            span,
            model=returned_model,
            cost_usd=cost_usd,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

        content = ""
        choices = payload.get("choices") or []
        if choices:
            content = choices[0].get("message", {}).get("content", "")

        # WSD-E4-T1 (Phase 3): the response may have come from an intra-tier
        # FALLBACK (LiteLLM router). Never silent (P8): detect it from the
        # headers and emit the degradation audit row. And a fallback does not
        # bypass policy (same rule as reassign): if NONE of the model group's
        # declared fallbacks is permitted by the tenant/stage policy, the
        # degraded response is refused at the boundary (P6) — the real cost
        # already incurred is still written to the ledger (honest accounting).
        degradation = failover.detect_degradation(model, resp.headers)
        if degradation is not None:
            permits = failover.audit_degradation(headers, degradation)
            if permits and not any(permits.values()):
                ledger.record_call(
                    tenant_id=headers.tenant_id,
                    work_item_id=headers.work_item_id,
                    stage=headers.stage.value,
                    task_class=headers.task_class,
                    model=returned_model,
                    cost_usd=cost_usd,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                )
                body = GatewayErrorResponse(
                    error="policy_denied",
                    message=(
                        f"response served by intra-tier fallback of '{model}' "
                        f"but no declared fallback is permitted by policy for "
                        f"stage={headers.stage.value} tenant={headers.tenant_id}"
                    ),
                    retryable=False,
                ).model_dump()
                body["kind"] = "fallback_model_not_allowed"
                body["fallback_candidates"] = degradation.fallback_candidates
                with _best_effort():
                    _audit_emit(
                        actor="system:model-gateway",
                        action="gateway.call_denied_policy",
                        tenant_id=headers.tenant_id,
                        work_item_id=headers.work_item_id,
                        details={
                            "stage": headers.stage.value,
                            "kind": "fallback_model_not_allowed",
                            "requested_model": model,
                            "fallback_candidates": degradation.fallback_candidates,
                        },
                    )
                span.set_status(trace_status_error())
                raise GatewayCallError(403, body)

        # WSD-E3-T4: record the REAL cost on the durable ledger (survives a
        # restart; it is the source for cost_export and budget accounting).
        # Only successful calls go into the ledger.
        ledger.record_call(
            tenant_id=headers.tenant_id,
            work_item_id=headers.work_item_id,
            stage=headers.stage.value,
            task_class=headers.task_class,
            model=returned_model,
            cost_usd=cost_usd,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

        return ChatCompletionResult(
            content=content,
            model=returned_model,
            cost_usd=cost_usd,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            raw=payload,
        )


def _extract_cost(resp: httpx.Response) -> float:
    raw = resp.headers.get(_COST_HEADER)
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


def _truncate_for_audit(body: dict, limit: int = 2000) -> str:
    """The upstream error body becomes a BOUNDED string in the audit details —
    enough evidence without bloating the ledger with arbitrary payloads."""
    import json as _json

    raw = _json.dumps(body, default=str)
    return raw if len(raw) <= limit else raw[:limit] + "...[truncated-for-audit]"


def _safe_json(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except Exception:
        return {"raw": resp.text}


def trace_status_error():
    from opentelemetry.trace import Status, StatusCode

    return Status(StatusCode.ERROR)


class _best_effort:
    """Context manager that silently swallows an optional validation failure
    (LiteLLM's error body may not match GatewayErrorResponse 100% — that must
    not break the handling of the real error, it only documents the contract
    expectation)."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is not None
