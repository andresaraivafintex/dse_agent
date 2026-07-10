"""model_gateway_client — biblioteca Python instalável do model-gateway
(WS-D). Superfície pública ESTÁVEL, importada por `sandbox_runtime` (WS-C):

    from model_gateway_client import mint_virtual_key, revoke_virtual_key

E, para quem for fazer a chamada de modelo em si (Coder session):

    from model_gateway_client import chat_completion, ChatCompletionResult
    from dse_contracts.gateway_contract import GatewayCallHeaders, Stage

Nada neste pacote importa um SDK de provider (`anthropic`, `boto3`, `openai`)
— só fala HTTP com o model-gateway (LiteLLM), nunca diretamente com um
provider. Ver tests/test_conformance_gateway_only.py.
"""
from __future__ import annotations

from .errors import GatewayCallError, ModelGatewayError, VirtualKeyNotFoundError
from .gateway_call import ChatCompletionResult, chat_completion
from .virtual_keys import IssuedVirtualKey, mint_virtual_key, revoke_virtual_key

__all__ = [
    "mint_virtual_key",
    "revoke_virtual_key",
    "IssuedVirtualKey",
    "chat_completion",
    "ChatCompletionResult",
    "ModelGatewayError",
    "VirtualKeyNotFoundError",
    "GatewayCallError",
]
