"""model_gateway_client exceptions. P6 decline-never-truncate: any failure
(LiteLLM admin call, model call, virtual key lookup) propagates as an explicit
exception at the boundary — never swallowed or silently "fixed"."""
from __future__ import annotations

from typing import Any


class ModelGatewayError(Exception):
    """Generic error while talking to the model-gateway (admin API or model call)."""


class VirtualKeyNotFoundError(ModelGatewayError):
    """`revoke_virtual_key` was called with a key this process / the
    `virtual_keys` table does not recognize — we do not guess, we fail clean."""


class GatewayCallError(ModelGatewayError):
    """The gateway answered with an error (policy denial / budget exhausted /
    model unavailable — `dse_contracts.gateway_contract.GatewayErrorResponse`
    — or any non-2xx HTTP). Carries the original body so whoever handles the
    error decides what to do (never decided by an LLM — P1)."""

    def __init__(self, status_code: int, body: dict[str, Any]):
        self.status_code = status_code
        self.body = body
        super().__init__(f"model-gateway call failed: HTTP {status_code}: {body}")
