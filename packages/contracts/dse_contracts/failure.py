"""Structured failure classes at the Activity→workflow boundary (Phase 2,
plano 09).

Before: the workflow classified a fail-closed refusal by SUBSTRING of the
message (`_FAIL_CLOSED_MARKERS`) — a transient error carrying the wrong word
killed the task. Now: whoever RAISES the failure declares the class in the
`type` of the `ApplicationError` (closed vocabulary below), and the workflow
decides by type — the message goes back to being human diagnostics only.

Rules:
  - `failure_type(FailureClass.x)` produces the canonical type
    ("dse.failure.x").
  - `parse_failure_type` ALSO recognizes the legacy types already recorded in
    Temporal histories (ProviderBillingError, EgressFailClosed) — replay of an
    in-flight workflow never misclassifies.
  - INFRA_TRANSIENT exists to explicitly label the retryable case; the
    mechanism is still Temporal's (retryable error + wall-clock ceiling) —
    never `non_retryable`.
"""
from __future__ import annotations

from enum import Enum

FAILURE_TYPE_PREFIX = "dse.failure."


class FailureClass(str, Enum):
    policy_fail_closed = "policy_fail_closed"  # egress down, expired vk, kill switch, policy denied
    budget_denied = "budget_denied"            # budget ceiling at admission/boundary
    provider_billing = "provider_billing"      # provider credits/billing/auth exhausted
    infra_transient = "infra_transient"        # retryable blip (gateway hiccup etc.)


# types already recorded in histories before the canonical vocabulary — the
# parser recognizes them forever (replay safety); NEVER remove entries here.
_LEGACY_TYPE_MAP: dict[str, FailureClass] = {
    "ProviderBillingError": FailureClass.provider_billing,
    "EgressFailClosed": FailureClass.policy_fail_closed,
}

# Classes the workflow turns into a clean fail-closed failure (P6) — the rest
# follow the normal retry/exhaustion path.
FAIL_CLOSED_CLASSES = frozenset(
    {FailureClass.policy_fail_closed, FailureClass.budget_denied, FailureClass.provider_billing}
)


def failure_type(kind: FailureClass) -> str:
    return f"{FAILURE_TYPE_PREFIX}{kind.value}"


def parse_failure_type(type_name: str | None) -> FailureClass | None:
    """ApplicationError type → FailureClass (canonical or legacy); None for any
    type outside the vocabulary (ordinary error, runtime exception etc.)."""
    if not type_name:
        return None
    if type_name in _LEGACY_TYPE_MAP:
        return _LEGACY_TYPE_MAP[type_name]
    if type_name.startswith(FAILURE_TYPE_PREFIX):
        try:
            return FailureClass(type_name[len(FAILURE_TYPE_PREFIX):])
        except ValueError:
            return None
    return None
