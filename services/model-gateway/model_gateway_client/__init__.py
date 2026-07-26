"""model_gateway_client — installable Python library for the model-gateway
(WS-D). STABLE public surface, imported by `sandbox_runtime` (WS-C):

    from model_gateway_client import mint_virtual_key, revoke_virtual_key

And, for whoever makes the model call itself (Coder session):

    from model_gateway_client import chat_completion, ChatCompletionResult
    from dse_contracts.gateway_contract import GatewayCallHeaders, Stage

Nothing in this package imports a provider SDK (`anthropic`, `boto3`,
`openai`) — it only speaks HTTP to the model-gateway (LiteLLM), never directly
to a provider. See tests/test_conformance_gateway_only.py.
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
from .failover import (
    Degradation,
    detect_degradation,
    intra_tier_failover_set,
    intra_tier_fallbacks,
)
from .gateway_call import ChatCompletionResult, chat_completion
from .policy import PolicyDecision, load_policies_from_file, resolve_policy
from .virtual_keys import IssuedVirtualKey, mint_virtual_key, revoke_virtual_key

__all__ = [
    # Phase 1 — stable surface (WS-C imports mint/revoke; Coder uses chat_completion)
    "mint_virtual_key",
    "revoke_virtual_key",
    "IssuedVirtualKey",
    "chat_completion",
    "ChatCompletionResult",
    "ModelGatewayError",
    "VirtualKeyNotFoundError",
    "GatewayCallError",
    # Phase 2 — WSD-E2 policy/budget at call time
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
    # Phase 2 — WSD-E4-T2 kill switch + reassign (WS-B/WS-F operator controls)
    "KillDecision",
    "is_killed",
    "set_kill_switch",
    "resolve_reassignment",
    "reassign_model",
    "clear_reassignment",
    # Phase 3 — WSD-E4-T1 intra-tier failover (declared degradation)
    "Degradation",
    "detect_degradation",
    "intra_tier_fallbacks",
    "intra_tier_failover_set",
]
