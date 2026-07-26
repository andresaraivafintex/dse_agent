"""WSD-E4-T1 (Phase 3) — INTRA-TIER failover and degradation.

The failover itself is NATIVE to the LiteLLM proxy (`router_settings.fallbacks`
in `litellm_config.yaml`): if a model group's primary deployment fails
(connection refused / 5xx / timeout), the router tries the declared fallback —
and only that one. This module is the CLIENT side of that story:

  1. `INTRA_TIER_FALLBACKS` — declarative mirror of the proxy's fallback map.
     It exists to (a) let us mint virtual keys covering the full failover set
     (a key scoped ONLY to the primary makes the proxy deny the fallback —
     correct server-side backstop behavior, but then there is no failover), and
     (b) allow the policy check on the model served under degradation.
     Consistency between this mirror and `litellm_config.yaml` is guaranteed by
     a test (test_failover_intra_tier.py) — if someone changes one without the
     other, CI breaks.

  2. `detect_degradation(...)` — deterministic detection that the response came
     from a fallback, using the headers LiteLLM returns
     (`x-litellm-attempted-fallbacks` > 0; `x-litellm-model-api-base`
     identifies the endpoint that actually served it).

  3. `audit_degradation(...)` — the degradation audit row (P8): failover is
     never silent. `gateway.call_degraded_fallback` with the serving endpoint,
     the fallback candidates and the policy verdict for each of them.

  4. Policy enforcement on the served model: if NONE of the model group's
     declared fallbacks is permitted by the tenant/stage policy, the degraded
     response is REFUSED at the boundary (P6) with `policy_denied`
     (kind=fallback_model_not_allowed) — a fallback does not bypass policy,
     same as reassign (WSD-E4-T2).

There is never a fallback route crossing tiers (NFR-07/P2): the negative test
`test_no_fallback_route_crosses_tier` parses `litellm_config.yaml` and fails if
any (primary, fallback) pair has a different `dse_tier`.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from dse_audit.client import emit as audit_emit
from dse_contracts.gateway_contract import GatewayCallHeaders

from . import policy

_AUDIT_ACTOR = "system:model-gateway"

# LiteLLM header: how many fallbacks were attempted up to this response.
ATTEMPTED_FALLBACKS_HEADER = "x-litellm-attempted-fallbacks"
# LiteLLM header: the api_base of the deployment that ACTUALLY served the response.
MODEL_API_BASE_HEADER = "x-litellm-model-api-base"

# Declarative mirror of router_settings.fallbacks from litellm_config.yaml
# (see docstring §1). Overridable by env var for other deployments.
_DEFAULT_FALLBACKS = {"eco/echo-model": ["eco/echo-model-b"]}


def intra_tier_fallbacks() -> dict[str, list[str]]:
    raw = os.environ.get("DSE_INTRA_TIER_FALLBACKS")
    if not raw:
        return dict(_DEFAULT_FALLBACKS)
    try:
        parsed = json.loads(raw)
        return {str(k): [str(m) for m in v] for k, v in parsed.items()}
    except (json.JSONDecodeError, TypeError, AttributeError):
        # Invalid config must not open a surprise fallback route: fall back to
        # the known default (fail closed onto the known map).
        return dict(_DEFAULT_FALLBACKS)


def intra_tier_failover_set(model: str) -> list[str]:
    """The full set of models a virtual key must cover for intra-tier failover
    to work: the primary + its declared fallbacks.

    This is what call sites (WS-C sessions) should pass in
    `mint_virtual_key(..., models=intra_tier_failover_set(model))` — a key
    scoped only to the primary makes the proxy (correctly) deny the fallback."""
    return [model, *intra_tier_fallbacks().get(model, [])]


@dataclass(frozen=True)
class Degradation:
    requested_model: str
    served_api_base: str | None
    attempted_fallbacks: int
    fallback_candidates: list[str]


def _fallback_api_bases() -> frozenset[str]:
    """api_bases of the known FALLBACK deployments (env DSE_FALLBACK_API_BASES,
    CSV) — mirror of the fallback api_bases in litellm_config. Opt-in: without
    the env var, cooldown detection is off and only the fallback header
    counts."""
    raw = os.environ.get("DSE_FALLBACK_API_BASES", "")
    return frozenset(b.strip() for b in raw.split(",") if b.strip())


def detect_degradation(requested_model: str, response_headers) -> Degradation | None:
    """Deterministic detection that the response did NOT come from the primary.
    Two paths, both auditable degradation (P8 — never silent):

    1. RUNTIME fallback: `x-litellm-attempted-fallbacks` > 0 (the router tried
       the primary, it failed, and it fell to the fallback on the same call).
    2. COOLDOWN fallback: attempted=0 but the `x-litellm-model-api-base` that
       served is a known FALLBACK api_base (DSE_FALLBACK_API_BASES) — the
       router's health-check had already pulled the primary out of the pool
       BEFORE this call, so it went straight to the fallback without "trying"
       the primary. Without this detection, a cooldown degradation would pass
       silently (found on the first real CI run, 2026-07-24: on the slow runner
       the health-check runs between stopping the primary and the call).
    """
    served = response_headers.get(MODEL_API_BASE_HEADER)
    raw = response_headers.get(ATTEMPTED_FALLBACKS_HEADER, "0")
    try:
        attempted = int(raw)
    except (TypeError, ValueError):
        attempted = 0

    degraded = attempted > 0 or (served is not None and served in _fallback_api_bases())
    if not degraded:
        return None
    return Degradation(
        requested_model=requested_model,
        served_api_base=served,
        attempted_fallbacks=attempted,
        fallback_candidates=intra_tier_fallbacks().get(requested_model, []),
    )


def audit_degradation(
    headers: GatewayCallHeaders, degradation: Degradation
) -> dict[str, bool]:
    """Emits the degradation audit row (P8 — failover is never silent) and
    returns the policy verdict per fallback candidate (used by the caller for
    the P6 enforcement — see gateway_call.chat_completion)."""
    decision = policy.resolve_policy(
        headers.tenant_id,
        headers.stage.value,
        data_class=headers.data_class,
        risk_class="*",
    )
    permits = {m: decision.permits(m) for m in degradation.fallback_candidates}
    audit_emit(
        actor=_AUDIT_ACTOR,
        action="gateway.call_degraded_fallback",
        tenant_id=headers.tenant_id,
        work_item_id=headers.work_item_id,
        details={
            "stage": headers.stage.value,
            "task_class": headers.task_class,
            "requested_model": degradation.requested_model,
            "served_api_base": degradation.served_api_base,
            "attempted_fallbacks": degradation.attempted_fallbacks,
            "fallback_candidates": degradation.fallback_candidates,
            "policy_permits_fallback": permits,
            "policy_source": decision.source,
        },
    )
    return permits
