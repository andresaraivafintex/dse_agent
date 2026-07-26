"""WSD-E4-T3 (Phase 3): model-path chaos battery — EXTENSION.

The egress-fail-closed / key-expiry / gateway-oscillation scenarios ALREADY
EXIST in `services/orchestrator/tests/test_chaos.py` (WSB-E5-T3b, Phase 2) and
prove the ORCHESTRATOR's behavior at the Activity boundary. This file does NOT
duplicate them — it adds the missing scenarios, proven against REAL infra
(LiteLLM + 2 echoes in Docker, the real egress-proxy on :8806, real Postgres):

  1. TOTAL provider outage (both echoes down, a genuine docker stop)
     -> clean refusal at the boundary (P6) + audit (P8), zero ledger rows;
  2. quota exhaustion (a real provider 429, simulated deterministically by the
     echo via a marker) -> 429 propagated as a clean boundary + audit;
  3. intra-tier failover under failure -> covered in
     test_failover_intra_tier.py (WSD-E4-T1 — same change set, pairs with this
     file);
  4. MID-TASK budget exhaustion -> call N completes IN FULL, call N+1 is
     refused at the boundary (never truncation in the middle of a response);
  5. egress to a non-allowlisted model endpoint -> denied by the REAL
     egress-proxy on port 8806 (default-deny), with a positive control proving
     that the ONLY permitted model route (model-gateway:4000) works through the
     SAME proxy.

Acceptance (WSD-E4-T3 plan): every scenario ends in an automatic retry (the
WS-B layer — Temporal), a Failed with a clear message, or an audited deny —
zero silent truncation.
"""
from __future__ import annotations

import json
import os

import httpx
import pytest
from dse_audit.client import get_connection as audit_conn
from dse_contracts.gateway_contract import GatewayCallHeaders, Stage
from model_gateway_client import (
    GatewayCallError,
    chat_completion,
    intra_tier_failover_set,
    mint_virtual_key,
    revoke_virtual_key,
    set_work_item_budget,
)
from model_gateway_client import ledger

from .chaos_helpers import (
    ECHO_MODEL,
    FALLBACK_ECHO_CONTAINER,
    PRIMARY_ECHO_CONTAINER,
    ensure_primary_serving,
    start_container,
    stop_container,
)

EGRESS_PROXY_URL = os.environ.get("DSE_EGRESS_PROXY_URL", "http://localhost:8806")


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


# ---------------------------------------------------------------------------
# Scenario 1 — TOTAL provider outage (both echoes down).
# ---------------------------------------------------------------------------


def test_total_provider_outage_clean_refusal_audited(unique_ids):
    """Primary AND fallback down (docker stop on both) -> the call fails CLEAN
    at the boundary (GatewayCallError with a clear message, which the WS-B
    workflow turns into an Activity retry/Failed), with an audit row (P8) and
    ZERO ledger rows (no phantom cost, no truncated output)."""
    t, wi = unique_ids["tenant_id"], unique_ids["work_item_id"]
    key = mint_virtual_key(t, wi, Stage.coder, models=intra_tier_failover_set(ECHO_MODEL))
    headers = GatewayCallHeaders(tenant_id=t, work_item_id=wi, stage=Stage.coder)
    try:
        ensure_primary_serving()
        stop_container(PRIMARY_ECHO_CONTAINER)
        stop_container(FALLBACK_ECHO_CONTAINER)
        try:
            with pytest.raises(GatewayCallError) as ei:
                chat_completion(
                    headers=headers, virtual_key=key, model=ECHO_MODEL,
                    messages=[{"role": "user", "content": "total-outage"}],
                    # measured in this session: with both containers stopped
                    # LiteLLM takes ~53s to burn through connect-timeouts ×
                    # retries × fallback before returning the final 500.
                    timeout=90.0,
                )
        finally:
            start_container(PRIMARY_ECHO_CONTAINER)
            start_container(FALLBACK_ECHO_CONTAINER)

        # clean boundary with a clear message (not an opaque crash). Depending
        # on how fast Docker tears down the container's network, LiteLLM
        # returns 408 (litellm.Timeout, 5s connect timeout per attempt) or 5xx
        # (connection refused) — both are typed refusals at the boundary.
        assert ei.value.status_code == 408 or ei.value.status_code >= 500
        assert "error" in ei.value.body

        # AUDITED deny/failure (P8)
        rows = _audit_rows(wi)
        upstream_failures = [d for a, d in rows if a == "gateway.call_failed_upstream"]
        assert len(upstream_failures) == 1
        assert upstream_failures[0]["model"] == ECHO_MODEL
        assert upstream_failures[0]["status_code"] >= 400

        # zero truncation / zero phantom cost: nothing in the ledger
        assert ledger.aggregate(tenant_id=t) == []
    finally:
        revoke_virtual_key(key)
        ensure_primary_serving()


# ---------------------------------------------------------------------------
# Scenario 2 — provider quota exhaustion (a real end-to-end 429).
# ---------------------------------------------------------------------------


def test_provider_quota_exhaustion_429_clean_boundary(unique_ids):
    """The echo answers 429 (OpenAI shape, deterministic via the marker) ->
    LiteLLM propagates RateLimitError -> the client raises GatewayCallError
    (429) at the boundary, audited. The automatic retry is the WS-B Activity's
    responsibility (Temporal) — here we prove the boundary is clean and typed,
    never a partial output."""
    t, wi = unique_ids["tenant_id"], unique_ids["work_item_id"]
    key = mint_virtual_key(t, wi, Stage.coder, models=intra_tier_failover_set(ECHO_MODEL))
    headers = GatewayCallHeaders(tenant_id=t, work_item_id=wi, stage=Stage.coder)
    try:
        ensure_primary_serving()
        with pytest.raises(GatewayCallError) as ei:
            chat_completion(
                headers=headers, virtual_key=key, model=ECHO_MODEL,
                messages=[{"role": "user", "content": "[[SIMULATE_QUOTA_EXHAUSTED]] do work"}],
                timeout=60.0,  # the router retries/falls back before giving up
            )

        assert ei.value.status_code == 429
        # clear message coming from the provider, not swallowed
        assert "quota" in json.dumps(ei.value.body).lower()

        rows = _audit_rows(wi)
        upstream_failures = [d for a, d in rows if a == "gateway.call_failed_upstream"]
        assert len(upstream_failures) == 1
        assert upstream_failures[0]["status_code"] == 429

        assert ledger.aggregate(tenant_id=t) == []  # a 429 does not become cost
    finally:
        revoke_virtual_key(key)


# ---------------------------------------------------------------------------
# Scenario 4 — MID-TASK budget exhaustion (boundary, never truncation).
# ---------------------------------------------------------------------------


def test_budget_exhaustion_mid_task_boundary_never_truncates(unique_ids):
    """Simulates a task in progress: a $1.00 cap; with $0.40 spent the next
    call COMPLETES in full (spend is only checked at the boundary, it never
    cuts a generation in progress); spend then passes the cap ($1.10) and the
    following call is refused CLEAN (402 budget_exhausted) + audit. Zero
    truncation at any point (P6)."""
    t, wi = unique_ids["tenant_id"], unique_ids["work_item_id"]
    set_work_item_budget(wi, t, 1.00)
    key = mint_virtual_key(t, wi, Stage.coder, models=intra_tier_failover_set(ECHO_MODEL))
    headers = GatewayCallHeaders(tenant_id=t, work_item_id=wi, stage=Stage.coder)
    try:
        ensure_primary_serving()
        # accumulated spend "for the task so far" (simulates earlier paid calls
        # — the echo costs $0, so the cost is injected into the SAME durable
        # ledger that enforcement reads).
        ledger.record_call(
            tenant_id=t, work_item_id=wi, stage="coder", task_class="default",
            model=ECHO_MODEL, cost_usd=0.40, tokens_in=100, tokens_out=50,
        )

        # under the cap -> the call passes and comes back COMPLETE (deterministic)
        result = chat_completion(
            headers=headers, virtual_key=key, model=ECHO_MODEL,
            messages=[{"role": "user", "content": "mid-task"}],
        )
        assert result.content == "ECHO[ksat-dim]"  # whole, never cut

        # the task "spends" more and blows past the cap ($0.40 + $0.70 = $1.10 > $1.00)
        ledger.record_call(
            tenant_id=t, work_item_id=wi, stage="coder", task_class="default",
            model=ECHO_MODEL, cost_usd=0.70, tokens_in=100, tokens_out=50,
        )

        # next BOUNDARY -> clean typed refusal + audit; nothing was truncated
        with pytest.raises(GatewayCallError) as ei:
            chat_completion(
                headers=headers, virtual_key=key, model=ECHO_MODEL,
                messages=[{"role": "user", "content": "one call too far"}],
            )
        assert ei.value.status_code == 402
        assert ei.value.body["error"] == "budget_exhausted"
        assert ei.value.body["kind"] == "work_item"
        # clear message: spend and cap spelled out for the human who reads the Failed
        assert "$" in ei.value.body["message"]

        actions = [a for a, _ in _audit_rows(wi)]
        assert "gateway.call_denied_budget" in actions
    finally:
        revoke_virtual_key(key)


# ---------------------------------------------------------------------------
# Scenario 5 — egress to a NON-allowlisted model endpoint (real proxy on :8806).
# ---------------------------------------------------------------------------


def _egress_proxy_up() -> bool:
    import socket

    host = EGRESS_PROXY_URL.split("://", 1)[-1].split(":")[0]
    port = int(EGRESS_PROXY_URL.rsplit(":", 1)[-1])
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


needs_egress_proxy = pytest.mark.skipif(
    not _egress_proxy_up(),
    reason="the real egress-proxy (WS-C) is not up on :8806 — bring up docker-compose.wsc.yml",
)


@needs_egress_proxy
def test_egress_denies_non_allowlisted_model_endpoint_plain_http():
    """failure mode 12: attempt to talk to a public model endpoint
    (api.openai.com) through the REAL egress-proxy -> 403 default-deny, with
    the request never leaving (the proxy does not even try to connect to the
    denied host)."""
    with httpx.Client(proxy=EGRESS_PROXY_URL, timeout=10.0) as client:
        resp = client.get("http://api.openai.com/v1/models")
    assert resp.status_code == 403


@needs_egress_proxy
def test_egress_denies_non_allowlisted_model_endpoint_https_connect():
    """Same failure mode over an HTTPS tunnel (CONNECT): the proxy refuses the
    tunnel to api.anthropic.com — the client sees a proxy error, never a
    handshake."""
    with httpx.Client(proxy=EGRESS_PROXY_URL, timeout=10.0) as client:
        with pytest.raises(httpx.ProxyError):
            client.get("https://api.anthropic.com/v1/models")


@needs_egress_proxy
def test_egress_allows_only_the_model_gateway_route():
    """Positive control: the ONLY permitted model route (model-gateway:4000,
    resolved INSIDE the Docker network by the proxy itself) works through the
    SAME proxy that denied the public endpoints above — proving the deny is not
    a broken proxy, it is default-deny policy working."""
    ensure_primary_serving()
    master = os.environ.get("DSE_LITELLM_MASTER_KEY", "sk-dse-local-dev-master-key")
    with httpx.Client(proxy=EGRESS_PROXY_URL, timeout=15.0) as client:
        resp = client.post(
            "http://model-gateway:4000/v1/chat/completions",
            headers={"Authorization": f"Bearer {master}"},
            json={
                "model": ECHO_MODEL,
                "messages": [{"role": "user", "content": "via-egress"}],
            },
        )
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "ECHO[sserge-aiv]"
