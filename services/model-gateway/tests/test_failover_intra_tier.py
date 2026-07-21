"""WSD-E4-T1 (Fase 3): failover e degradação INTRA-TIER.

Testes REAIS contra o LiteLLM proxy + 2 instâncias do modelo eco em Docker.
O failover é provado DE VERDADE: `docker stop` no container primário
(`dse_model_gateway_echo`) e a chamada seguinte é servida pela instância B
(`dse_model_gateway_echo_b`) via `router_settings.fallbacks` nativo do proxy.

Provam:
  - primário fora -> fallback assume; a resposta é COMPLETA (determinística,
    nunca truncada — P6), o custo/atribuição continuam corretos (linha real no
    ledger durável para o tenant/work_item) e a degradação NUNCA é silenciosa:
    audit row `gateway.call_degraded_fallback` com o endpoint que serviu (P8);
  - primário saudável -> zero audit de degradação (sem falso positivo);
  - fallback NÃO burla política (mesma regra do reassign): se a política do
    tenant não permite nenhum fallback declarado, a resposta degradada é
    recusada na fronteira com `policy_denied`/`fallback_model_not_allowed`;
  - teste negativo declarativo (aceitação do WSD-E4-T1): NENHUMA rota de
    fallback do litellm_config.yaml cruza o tier contratado (NFR-07/P2), e o
    espelho `failover.intra_tier_fallbacks()` do cliente é consistente com o
    mapa do proxy (se alguém mudar um sem o outro, este arquivo quebra).

Nota (achado empírico desta sessão, LiteLLM 1.93.0): o fallback do router
acontece MESMO com uma virtual key escopada só no modelo primário — o
model-scoping da key não restringe o alvo de fallback. Por isso o check de
política do modelo servido é feito no cliente (gateway_call) e o espelho
declarativo existe; ver README §Fase 3.
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
from model_gateway_client import db, failover, ledger, policy

from .chaos_helpers import (
    ECHO_MODEL,
    ECHO_MODEL_B,
    FALLBACK_API_BASE,
    PRIMARY_ECHO_CONTAINER,
    ensure_primary_serving,
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
# Testes declarativos (config) — rodam sem derrubar nada.
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
    """O espelho declarativo do cliente (failover.intra_tier_fallbacks) e o
    mapa do proxy (router_settings.fallbacks) são o MESMO mapa — mudar um sem
    o outro quebra aqui."""
    assert _proxy_fallback_map(_load_litellm_config()) == intra_tier_fallbacks()


def test_no_fallback_route_crosses_tier():
    """Aceitação WSD-E4-T1 (teste negativo): nenhuma rota de fallback cruza o
    tier contratado (NFR-07/P2 — nunca Tier 2 -> Tier 1 nem para endpoint
    público). Parseia o config declarativo real do proxy."""
    config = _load_litellm_config()
    tiers = {
        m["model_name"]: m.get("model_info", {}).get("dse_tier")
        for m in config["model_list"]
    }
    routes = _proxy_fallback_map(config)
    assert routes, "esperava ao menos 1 rota de fallback intra-tier configurada"
    for primary, fallbacks in routes.items():
        assert primary in tiers, f"rota de fallback para modelo não registrado: {primary}"
        for fb in fallbacks:
            assert fb in tiers, f"fallback não registrado no model_list: {fb}"
            assert tiers[fb] == tiers[primary], (
                f"ROTA DE FALLBACK CRUZA TIER: {primary} (tier={tiers[primary]}) "
                f"-> {fb} (tier={tiers[fb]}) — viola NFR-07/P2"
            )


def test_failover_set_covers_primary_and_fallbacks():
    assert intra_tier_failover_set(ECHO_MODEL) == [ECHO_MODEL, ECHO_MODEL_B]
    # modelo sem fallback declarado -> conjunto é só ele mesmo
    assert intra_tier_failover_set("bedrock/anthropic.claude-3-haiku") == [
        "bedrock/anthropic.claude-3-haiku"
    ]


# ---------------------------------------------------------------------------
# Testes de chaos REAL (docker stop no primário).
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
    """O teste central do WSD-E4-T1: derruba o primário DE VERDADE; o fallback
    intra-tier assume; resposta completa (P6); custo/atribuição corretos no
    ledger durável; audit row de degradação (P8)."""
    t, wi = unique_ids["tenant_id"], unique_ids["work_item_id"]
    key = mint_virtual_key(t, wi, Stage.coder, models=intra_tier_failover_set(ECHO_MODEL))
    headers = GatewayCallHeaders(
        tenant_id=t, work_item_id=wi, stage=Stage.coder, task_class="feature"
    )
    try:
        ensure_primary_serving()
        stop_container(PRIMARY_ECHO_CONTAINER)
        try:
            result = chat_completion(
                headers=headers, virtual_key=key, model=ECHO_MODEL,
                messages=[{"role": "user", "content": "failover-now"}],
                timeout=90.0,  # detecção de falha + retry + fallback do router
            )
        finally:
            start_container(PRIMARY_ECHO_CONTAINER)

        # resposta COMPLETA e determinística — o eco B produz o mesmo texto do
        # primário (nada truncado/mascarado, P6).
        assert result.content == "ECHO[won-revoliaf]"

        # atribuição de custo continua correta: 1 linha real no ledger durável
        # para o MESMO tenant/work_item/stage/task_class da chamada.
        rows = ledger.aggregate(tenant_id=t)
        assert len(rows) == 1
        assert rows[0]["call_count"] == 1
        assert rows[0]["task_class"] == "feature"
        assert rows[0]["stage"] == "coder"

        # degradação auditada (P8): nunca silenciosa, com o endpoint que serviu.
        rows_audit = _audit_rows(wi)
        degradations = [d for a, d in rows_audit if a == "gateway.call_degraded_fallback"]
        assert len(degradations) == 1
        det = degradations[0]
        assert det["requested_model"] == ECHO_MODEL
        assert det["served_api_base"] == FALLBACK_API_BASE
        assert det["attempted_fallbacks"] >= 1
        assert det["fallback_candidates"] == [ECHO_MODEL_B]
        assert det["policy_permits_fallback"] == {ECHO_MODEL_B: True}
    finally:
        revoke_virtual_key(key)
        ensure_primary_serving()


def test_fallback_does_not_bypass_policy(unique_ids):
    """Política do tenant permite SÓ o primário -> com o primário fora, a
    resposta degradada (servida pelo fallback) é RECUSADA na fronteira (P6)
    com policy_denied/fallback_model_not_allowed + audit (P8). O custo real
    incorrido ainda é gravado no ledger (accounting honesto)."""
    t, wi = unique_ids["tenant_id"], unique_ids["work_item_id"]
    _insert_policy(t, "coder", [ECHO_MODEL])  # fallback NÃO permitido
    key = mint_virtual_key(t, wi, Stage.coder, models=intra_tier_failover_set(ECHO_MODEL))
    headers = GatewayCallHeaders(tenant_id=t, work_item_id=wi, stage=Stage.coder)
    try:
        ensure_primary_serving()
        stop_container(PRIMARY_ECHO_CONTAINER)
        try:
            with pytest.raises(GatewayCallError) as ei:
                chat_completion(
                    headers=headers, virtual_key=key, model=ECHO_MODEL,
                    messages=[{"role": "user", "content": "denied-degraded"}],
                    timeout=90.0,  # detecção de falha + retry + fallback do router
                )
        finally:
            start_container(PRIMARY_ECHO_CONTAINER)

        assert ei.value.status_code == 403
        assert ei.value.body["error"] == "policy_denied"
        assert ei.value.body["kind"] == "fallback_model_not_allowed"

        rows_audit = _audit_rows(wi)
        actions = [a for a, _ in rows_audit]
        assert "gateway.call_degraded_fallback" in actions  # degradação registrada
        assert "gateway.call_denied_policy" in actions      # e a recusa também
        # accounting honesto: a chamada degradada custou de verdade -> ledger.
        rows = ledger.aggregate(tenant_id=t)
        assert len(rows) == 1 and rows[0]["call_count"] == 1
    finally:
        revoke_virtual_key(key)
        ensure_primary_serving()
