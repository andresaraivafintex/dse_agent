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

from .budget import BudgetCaps, BudgetStatus, get_status, resolve_caps, set_work_item_budget
from .controls import (
    KillDecision,
    clear_reassignment,
    is_killed,
    reassign_model,
    resolve_reassignment,
    set_kill_switch,
)
from .enforcement import EnforcementResult, enforce_call
from .errors import GatewayCallError, ModelGatewayError, VirtualKeyNotFoundError
from .gateway_call import ChatCompletionResult, chat_completion
from .policy import PolicyDecision, load_policies_from_file, resolve_policy
from .virtual_keys import IssuedVirtualKey, mint_virtual_key, revoke_virtual_key

__all__ = [
    # Fase 1 — superfície estável (WS-C importa mint/revoke; Coder usa chat_completion)
    "mint_virtual_key",
    "revoke_virtual_key",
    "IssuedVirtualKey",
    "chat_completion",
    "ChatCompletionResult",
    "ModelGatewayError",
    "VirtualKeyNotFoundError",
    "GatewayCallError",
    # Fase 2 — WSD-E2 policy/budget no call time
    "PolicyDecision",
    "resolve_policy",
    "load_policies_from_file",
    "BudgetCaps",
    "BudgetStatus",
    "get_status",
    "resolve_caps",
    "set_work_item_budget",
    "EnforcementResult",
    "enforce_call",
    # Fase 2 — WSD-E4-T2 kill switch + reassign (controles de operador WS-B/WS-F)
    "KillDecision",
    "is_killed",
    "set_kill_switch",
    "resolve_reassignment",
    "reassign_model",
    "clear_reassignment",
]
