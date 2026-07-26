"""WSD-E4-T1 (Phase 3): INTRA-TIER failover and degradation.

REAL tests against the LiteLLM proxy + 2 echo model instances in Docker. The
failover is proven FOR REAL: `docker stop` on the primary container
(`dse_model_gateway_echo`) and the next call is served by instance B
(`dse_model_gateway_echo_b`) through the proxy's native
`router_settings.fallbacks`.

They prove:
  - primary down -> the fallback takes over; the response is COMPLETE
    (deterministic, never truncated — P6), cost/attribution stay correct (a real
    row on the durable ledger for the tenant/work_item) and the degradation is
    NEVER silent: a `gateway.call_degraded_fallback` audit row with the endpoint
    that served it (P8);
  - healthy primary -> zero degradation audit (no false positive);
  - a fallback does NOT bypass policy (same rule as reassign): if the tenant's
    policy permits no declared fallback, the degraded response is refused at the
    boundary with `policy_denied`/`fallback_model_not_allowed`;
  - declarative negative test (WSD-E4-T1 acceptance): NO fallback route in
    litellm_config.yaml crosses the contracted tier (NFR-07/P2), and the
    client's `failover.intra_tier_fallbacks()` mirror is consistent with the
    proxy's map (if someone changes one without the other, this file breaks).

Note (empirical finding from this session, LiteLLM 1.93.0): the router's
fallback happens EVEN with a virtual key scoped only to the primary model — the
key's model-scoping does not restrict the fallback target. That is why the
policy check on the served model is done in the client (gateway_call) and why
the declarative mirror exists; see README §Fase 3.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from dse_audit.client import get_connection as audit_conn
from dse_contracts.gateway_contract import GatewayCallHeaders, Stage
from model_gateway_client import (
    GatewayCallError,
    chat_completion,
    intra_tier_failover_set,
    intra_tier_fallbacks,
    mint_virtual_key,
    revoke_virtual_key,
)
from model_gateway_client import db, ledger, policy

from .chaos_helpers import (
    ECHO_MODEL,
    ECHO_MODEL_B,
    FALLBACK_API_BASE,
    PRIMARY_ECHO_CONTAINER,
    ensure_primary_serving,
    wait_until_fallback_serves,
    start_container,
    stop_container,
)

_LITELLM_CONFIG = Path(__file__).resolve().parent.parent / "litellm_config.yaml"


def _audit_rows(work_item_id):
    conn = audit_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT action, details FROM audit_log WHERE work_item_id=%s ORDER BY id",
                (work_item_id,),
            )
            return [(a, d if isinstance(d, dict) else json.loads(d)) for a, d in cur.fetchall()]
    finally:
        conn.close()


def _insert_policy(tenant_id, stage, allowed_models):
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO model_policies
                    (tenant_id, stage, data_class, risk_class, allowed_models, priority)
                VALUES (%s,%s,'*','*',%s::jsonb,0)
                ON CONFLICT (tenant_id, stage, data_class, risk_class)
                DO UPDATE SET allowed_models=EXCLUDED.allowed_models, is_active=true
                """,
                (tenant_id, stage, json.dumps(allowed_models)),
            )
        conn.commit()
    finally:
        conn.close()
    policy.clear_cache()


# ---------------------------------------------------------------------------
# Declarative (config) tests — they run without taking anything down.
# ---------------------------------------------------------------------------


def _load_litellm_config() -> dict:
    return yaml.safe_load(_LITELLM_CONFIG.read_text())


def _proxy_fallback_map(config: dict) -> dict[str, list[str]]:
    routes: dict[str, list[str]] = {}
    for entry in (config.get("router_settings") or {}).get("fallbacks", []) or []:
        for primary, fallbacks in entry.items():
            routes[primary] = list(fallbacks)
    return routes


def test_client_fallback_mirror_matches_litellm_config():
    """The client's declarative mirror (failover.intra_tier_fallbacks) and the
    proxy's map (router_settings.fallbacks) are the SAME map — changing one
    without the other breaks here."""
    assert _proxy_fallback_map(_load_litellm_config()) == intra_tier_fallbacks()


def test_no_fallback_route_crosses_tier():
    """WSD-E4-T1 acceptance (negative test): no fallback route crosses the
    contracted tier (NFR-07/P2 — never Tier 2 -> Tier 1, nor to a public
    endpoint). Parses the proxy's real declarative config."""
    config = _load_litellm_config()
    tiers = {
        m["model_name"]: m.get("model_info", {}).get("dse_tier")
        for m in config["model_list"]
    }
    routes = _proxy_fallback_map(config)
    assert routes, "esperava ao menos 1 rota de fallback intra-tier configurada"
    for primary, fallbacks in routes.items():
        assert primary in tiers, f"fallback route for an unregistered model: {primary}"
        for fb in fallbacks:
            assert fb in tiers, f"fallback not registered in model_list: {fb}"
            assert tiers[fb] == tiers[primary], (
                f"ROTA DE FALLBACK CRUZA TIER: {primary} (tier={tiers[primary]}) "
                f"-> {fb} (tier={tiers[fb]}) — viola NFR-07/P2"
            )


def test_failover_set_covers_primary_and_fallbacks():
    assert intra_tier_failover_set(ECHO_MODEL) == [ECHO_MODEL, ECHO_MODEL_B]
    # a model with no declared fallback -> the set is just itself
    assert intra_tier_failover_set("bedrock/anthropic.claude-3-haiku") == [
        "bedrock/anthropic.claude-3-haiku"
    ]


# ---------------------------------------------------------------------------
# REAL chaos tests (docker stop on the primary).
# ---------------------------------------------------------------------------


def test_primary_healthy_no_degradation_audit(unique_ids):
    t, wi = unique_ids["tenant_id"], unique_ids["work_item_id"]
    key = mint_virtual_key(t, wi, Stage.coder, models=intra_tier_failover_set(ECHO_MODEL))
    headers = GatewayCallHeaders(tenant_id=t, work_item_id=wi, stage=Stage.coder)
    try:
        ensure_primary_serving()
        result = chat_completion(
            headers=headers, virtual_key=key, model=ECHO_MODEL,
            messages=[{"role": "user", "content": "healthy"}],
        )
        assert result.content == "ECHO[yhtlaeh]"
        actions = [a for a, _ in _audit_rows(wi)]
        assert "gateway.call_degraded_fallback" not in actions
    finally:
        revoke_virtual_key(key)


def test_primary_down_fallback_serves_with_audit_and_correct_attribution(unique_ids):
    """WSD-E4-T1's central test: takes the primary down FOR REAL; the intra-tier
    fallback takes over; complete response (P6); correct cost/attribution on the
    durable ledger; degradation audit row (P8)."""
    t, wi = unique_ids["tenant_id"], unique_ids["work_item_id"]
    key = mint_virtual_key(t, wi, Stage.coder, models=intra_tier_failover_set(ECHO_MODEL))
    headers = GatewayCallHeaders(
        tenant_id=t, work_item_id=wi, stage=Stage.coder, task_class="feature"
    )
    try:
        ensure_primary_serving()
        stop_container(PRIMARY_ECHO_CONTAINER)
        try:
            # Determinism: only call after the router has CONFIRMED the
            # fallback is serving (otherwise the primary could still be up —
            # race).
            wait_until_fallback_serves()
            result = chat_completion(
                headers=headers, virtual_key=key, model=ECHO_MODEL,
                messages=[{"role": "user", "content": "failover-now"}],
                timeout=90.0,  # failure detection + retry + router fallback
            )
        finally:
            start_container(PRIMARY_ECHO_CONTAINER)

        # COMPLETE and deterministic response — echo B produces the same text
        # as the primary (nothing truncated/masked, P6).
        assert result.content == "ECHO[won-revoliaf]"

        # cost attribution stays correct: 1 real row on the durable ledger for
        # the SAME tenant/work_item/stage/task_class of the call.
        rows = ledger.aggregate(tenant_id=t)
        assert len(rows) == 1
        assert rows[0]["call_count"] == 1
        assert rows[0]["task_class"] == "feature"
        assert rows[0]["stage"] == "coder"

        # audited degradation (P8): never silent, with the endpoint that served.
        rows_audit = _audit_rows(wi)
        degradations = [d for a, d in rows_audit if a == "gateway.call_degraded_fallback"]
        assert len(degradations) == 1
        det = degradations[0]
        assert det["requested_model"] == ECHO_MODEL
        # The REAL invariant: the fallback (echo-b) served. attempted_fallbacks
        # is >=1 on a runtime fallback OR 0 on cooldown (the primary was already
        # out of the pool after the wait) — both are auditable degradation (P8).
        assert det["served_api_base"] == FALLBACK_API_BASE
        assert det["attempted_fallbacks"] >= 0
        assert det["fallback_candidates"] == [ECHO_MODEL_B]
        assert det["policy_permits_fallback"] == {ECHO_MODEL_B: True}
    finally:
        revoke_virtual_key(key)
        ensure_primary_serving()


def test_fallback_does_not_bypass_policy(unique_ids):
    """The tenant's policy permits ONLY the primary -> with the primary down,
    the degraded response (served by the fallback) is REFUSED at the boundary
    (P6) with policy_denied/fallback_model_not_allowed + audit (P8). The real
    cost incurred is still written to the ledger (honest accounting)."""
    t, wi = unique_ids["tenant_id"], unique_ids["work_item_id"]
    _insert_policy(t, "coder", [ECHO_MODEL])  # fallback NOT permitted
    key = mint_virtual_key(t, wi, Stage.coder, models=intra_tier_failover_set(ECHO_MODEL))
    headers = GatewayCallHeaders(tenant_id=t, work_item_id=wi, stage=Stage.coder)
    try:
        ensure_primary_serving()
        stop_container(PRIMARY_ECHO_CONTAINER)
        try:
            # Determinism: fallback confirmed serving before the call.
            wait_until_fallback_serves()
            with pytest.raises(GatewayCallError) as ei:
                chat_completion(
                    headers=headers, virtual_key=key, model=ECHO_MODEL,
                    messages=[{"role": "user", "content": "denied-degraded"}],
                    timeout=90.0,  # failure detection + retry + router fallback
                )
        finally:
            start_container(PRIMARY_ECHO_CONTAINER)

        assert ei.value.status_code == 403
        assert ei.value.body["error"] == "policy_denied"
        assert ei.value.body["kind"] == "fallback_model_not_allowed"

        rows_audit = _audit_rows(wi)
        actions = [a for a, _ in rows_audit]
        assert "gateway.call_degraded_fallback" in actions  # degradation recorded
        assert "gateway.call_denied_policy" in actions      # and the refusal too
        # honest accounting: the degraded call really cost money -> ledger.
        rows = ledger.aggregate(tenant_id=t)
        assert len(rows) == 1 and rows[0]["call_count"] == 1
    finally:
        revoke_virtual_key(key)
        ensure_primary_serving()


# --- Unit: COOLDOWN degradation detection (no docker/race) -------------------
# Deterministic proof of the logic the integration test exercises under the
# runner's timing: the primary out of the pool (health-check) -> the fallback
# serves DIRECTLY (attempted_fallbacks=0), and that MUST count as auditable
# degradation.

def test_detect_degradation_runtime_fallback():
    from model_gateway_client.failover import detect_degradation
    deg = detect_degradation(ECHO_MODEL, {
        "x-litellm-attempted-fallbacks": "1",
        "x-litellm-model-api-base": FALLBACK_API_BASE,
    })
    assert deg is not None and deg.attempted_fallbacks == 1


def test_detect_degradation_cooldown_direct_to_fallback(monkeypatch):
    monkeypatch.setenv("DSE_FALLBACK_API_BASES", FALLBACK_API_BASE)
    from model_gateway_client.failover import detect_degradation
    # attempted=0 (primary already out of the pool) but SERVED by the fallback -> degradation
    deg = detect_degradation(ECHO_MODEL, {
        "x-litellm-attempted-fallbacks": "0",
        "x-litellm-model-api-base": FALLBACK_API_BASE,
    })
    assert deg is not None
    assert deg.attempted_fallbacks == 0
    assert deg.served_api_base == FALLBACK_API_BASE


def test_detect_degradation_primary_is_not_degradation(monkeypatch):
    monkeypatch.setenv("DSE_FALLBACK_API_BASES", FALLBACK_API_BASE)
    from model_gateway_client.failover import detect_degradation
    from .chaos_helpers import PRIMARY_API_BASE
    assert detect_degradation(ECHO_MODEL, {
        "x-litellm-attempted-fallbacks": "0",
        "x-litellm-model-api-base": PRIMARY_API_BASE,
    }) is None
