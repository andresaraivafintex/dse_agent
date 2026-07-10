"""Único caminho de chamada de modelo do lado do cliente (WSD-E1-T4 /
WSD-E3-T1). Todo agente (Coder, e futuramente Planner/Tester/Reviewer)
chama `chat_completion` — nunca um SDK de provider (`anthropic`, `boto3`)
diretamente. Isso é o que faz o `test_conformance_gateway_only.py` provar.

Usa `dse_contracts.gateway_contract.GatewayCallHeaders` (contrato já
publicado pela fundação) para os headers obrigatórios de policy/budget
enforcement no call time — mas ESTE módulo não decide nem aplica policy
(isso é WSD-E2, Fase 2). Ele só propaga os headers e trata a resposta.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from dse_contracts.gateway_contract import GatewayCallHeaders, GatewayErrorResponse

from . import settings, telemetry
from .errors import GatewayCallError

# Header que o LiteLLM usa para reportar o custo real da chamada (calculado
# por ele a partir do model + tokens — nunca recalculado por nós).
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
    """Chama `POST {gateway_base_url}/v1/chat/completions`. Nunca chama
    nenhum outro host — `settings.gateway_base_url()` é a única base URL
    usada por este módulo (ver teste de conformidade)."""
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
            raise GatewayCallError(0, {"error": "transport_error", "message": str(exc)}) from exc

        if resp.status_code >= 300:
            span.set_status(trace_status_error())
            error_body = _safe_json(resp)
            # Fronteira P6: falha limpa, nunca "continuamos mesmo assim".
            # Se o corpo casa com o contrato de erro publicado, valida contra
            # ele (documenta o shape esperado; não muda o comportamento).
            with _best_effort():
                GatewayErrorResponse.model_validate(error_body)
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


def _safe_json(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except Exception:
        return {"raw": resp.text}


def trace_status_error():
    from opentelemetry.trace import Status, StatusCode

    return Status(StatusCode.ERROR)


class _best_effort:
    """Context manager que ignora silenciosamente falha de validação
    opcional (o corpo de erro do LiteLLM pode não bater 100% com
    GatewayErrorResponse — isso não deve derrubar o tratamento de erro real,
    só documenta a expectativa do contrato)."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is not None
