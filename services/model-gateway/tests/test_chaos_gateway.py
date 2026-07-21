"""WSD-E4-T3 (Fase 3): bateria de chaos do caminho de modelo — EXTENSÃO.

Os cenários egress-fail-closed / key-expiry / gateway-oscillation JÁ EXISTEM
em `services/orchestrator/tests/test_chaos.py` (WSB-E5-T3b, Fase 2) e provam
o comportamento do ORQUESTRADOR na fronteira de Activity. Este arquivo NÃO os
duplica — adiciona os cenários que faltavam, provados contra a infra REAL
(LiteLLM + 2 ecos em Docker, egress-proxy real na :8806, Postgres real):

  1. outage TOTAL de provider (ambos os ecos fora, docker stop de verdade)
     -> recusa limpa na fronteira (P6) + audit (P8), zero linha no ledger;
  2. exaustão de quota (429 real do provider, simulado deterministicamente
     pelo eco via marcador) -> 429 propagado como fronteira limpa + audit;
  3. failover intra-tier sob falha -> coberto em test_failover_intra_tier.py
     (WSD-E4-T1 — mesmo conjunto de mudanças, liga com este arquivo);
  4. exaustão de budget MID-TASK -> a chamada N completa INTEIRA, a chamada
     N+1 é recusada na fronteira (nunca truncamento no meio de uma resposta);
  5. egress a endpoint de modelo não-allowlisted -> negado pelo egress-proxy
     REAL da porta 8806 (default-deny), com controle positivo provando que a
     ÚNICA rota de modelo permitida (model-gateway:4000) funciona através do
     MESMO proxy.

Aceitação (plano WSD-E4-T3): todos os cenários terminam em retry automático
(camada do WS-B — Temporal), Failed com mensagem clara, ou deny auditado —
zero truncamento silencioso.
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
# Cenário 1 — outage TOTAL de provider (ambos os ecos fora).
# ---------------------------------------------------------------------------


def test_total_provider_outage_clean_refusal_audited(unique_ids):
    """Primário E fallback fora (docker stop nos dois) -> a chamada falha
    LIMPA na fronteira (GatewayCallError com mensagem clara, que o workflow do
    WS-B converte em retry de Activity/Failed), com audit row (P8) e ZERO
    linha no ledger (nenhum custo fantasma, nenhum output truncado)."""
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
                    # medido nesta sessão: com ambos os containers parados o
                    # LiteLLM leva ~53s para esgotar connect-timeouts ×
                    # retries × fallback antes de devolver o 500 final.
                    timeout=90.0,
                )
        finally:
            start_container(PRIMARY_ECHO_CONTAINER)
            start_container(FALLBACK_ECHO_CONTAINER)

        # fronteira limpa com mensagem clara (não um crash opaco). Dependendo
        # de quão rápido o Docker derruba a rede do container, o LiteLLM
        # devolve 408 (litellm.Timeout, connect timeout de 5s por tentativa)
        # ou 5xx (connection refused) — ambos são recusas tipadas na fronteira.
        assert ei.value.status_code == 408 or ei.value.status_code >= 500
        assert "error" in ei.value.body

        # deny/failure AUDITADO (P8)
        rows = _audit_rows(wi)
        upstream_failures = [d for a, d in rows if a == "gateway.call_failed_upstream"]
        assert len(upstream_failures) == 1
        assert upstream_failures[0]["model"] == ECHO_MODEL
        assert upstream_failures[0]["status_code"] >= 400

        # zero truncamento / zero custo fantasma: nada no ledger
        assert ledger.aggregate(tenant_id=t) == []
    finally:
        revoke_virtual_key(key)
        ensure_primary_serving()


# ---------------------------------------------------------------------------
# Cenário 2 — exaustão de quota do provider (429 real fim-a-fim).
# ---------------------------------------------------------------------------


def test_provider_quota_exhaustion_429_clean_boundary(unique_ids):
    """O eco responde 429 (shape OpenAI, determinístico via marcador) ->
    o LiteLLM propaga RateLimitError -> o cliente levanta GatewayCallError
    (429) na fronteira, auditado. O retry automático é responsabilidade da
    Activity do WS-B (Temporal) — aqui provamos que a fronteira é limpa e
    tipada, nunca um output parcial."""
    t, wi = unique_ids["tenant_id"], unique_ids["work_item_id"]
    key = mint_virtual_key(t, wi, Stage.coder, models=intra_tier_failover_set(ECHO_MODEL))
    headers = GatewayCallHeaders(tenant_id=t, work_item_id=wi, stage=Stage.coder)
    try:
        ensure_primary_serving()
        with pytest.raises(GatewayCallError) as ei:
            chat_completion(
                headers=headers, virtual_key=key, model=ECHO_MODEL,
                messages=[{"role": "user", "content": "[[SIMULATE_QUOTA_EXHAUSTED]] do work"}],
                timeout=60.0,  # o router retenta/faz fallback antes de desistir
            )

        assert ei.value.status_code == 429
        # mensagem clara vinda do provider, não engolida
        assert "quota" in json.dumps(ei.value.body).lower()

        rows = _audit_rows(wi)
        upstream_failures = [d for a, d in rows if a == "gateway.call_failed_upstream"]
        assert len(upstream_failures) == 1
        assert upstream_failures[0]["status_code"] == 429

        assert ledger.aggregate(tenant_id=t) == []  # 429 não vira custo
    finally:
        revoke_virtual_key(key)


# ---------------------------------------------------------------------------
# Cenário 4 — exaustão de budget MID-TASK (fronteira, nunca truncamento).
# ---------------------------------------------------------------------------


def test_budget_exhaustion_mid_task_boundary_never_truncates(unique_ids):
    """Simula uma tarefa em andamento: cap de $1.00; com $0.40 gastos a
    chamada seguinte COMPLETA inteira (o gasto só é checado na fronteira,
    nunca corta uma geração em curso); o gasto passa do cap ($1.10) e a
    chamada seguinte é recusada LIMPA (402 budget_exhausted) + audit. Zero
    truncamento em qualquer ponto (P6)."""
    t, wi = unique_ids["tenant_id"], unique_ids["work_item_id"]
    set_work_item_budget(wi, t, 1.00)
    key = mint_virtual_key(t, wi, Stage.coder, models=intra_tier_failover_set(ECHO_MODEL))
    headers = GatewayCallHeaders(tenant_id=t, work_item_id=wi, stage=Stage.coder)
    try:
        ensure_primary_serving()
        # gasto acumulado "da tarefa até aqui" (simula chamadas pagas
        # anteriores — o eco custa $0, então o custo é injetado no MESMO
        # ledger durável que o enforcement lê).
        ledger.record_call(
            tenant_id=t, work_item_id=wi, stage="coder", task_class="default",
            model=ECHO_MODEL, cost_usd=0.40, tokens_in=100, tokens_out=50,
        )

        # abaixo do cap -> a chamada passa e vem COMPLETA (determinística)
        result = chat_completion(
            headers=headers, virtual_key=key, model=ECHO_MODEL,
            messages=[{"role": "user", "content": "mid-task"}],
        )
        assert result.content == "ECHO[ksat-dim]"  # inteira, nunca cortada

        # a tarefa "gasta" mais e estoura o cap ($0.40 + $0.70 = $1.10 > $1.00)
        ledger.record_call(
            tenant_id=t, work_item_id=wi, stage="coder", task_class="default",
            model=ECHO_MODEL, cost_usd=0.70, tokens_in=100, tokens_out=50,
        )

        # próxima FRONTEIRA -> recusa limpa tipada + audit; nada foi truncado
        with pytest.raises(GatewayCallError) as ei:
            chat_completion(
                headers=headers, virtual_key=key, model=ECHO_MODEL,
                messages=[{"role": "user", "content": "one call too far"}],
            )
        assert ei.value.status_code == 402
        assert ei.value.body["error"] == "budget_exhausted"
        assert ei.value.body["kind"] == "work_item"
        # mensagem clara: gasto e cap explícitos para o humano que vai ler o Failed
        assert "$" in ei.value.body["message"]

        actions = [a for a, _ in _audit_rows(wi)]
        assert "gateway.call_denied_budget" in actions
    finally:
        revoke_virtual_key(key)


# ---------------------------------------------------------------------------
# Cenário 5 — egress a endpoint de modelo NÃO-allowlisted (proxy real :8806).
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
    reason="egress-proxy real (WS-C) não está no ar na :8806 — suba docker-compose.wsc.yml",
)


@needs_egress_proxy
def test_egress_denies_non_allowlisted_model_endpoint_plain_http():
    """failure mode 12: tentativa de falar com um endpoint de modelo público
    (api.openai.com) através do egress-proxy REAL -> 403 default-deny, sem a
    requisição jamais sair (o proxy nem tenta conectar no host negado)."""
    with httpx.Client(proxy=EGRESS_PROXY_URL, timeout=10.0) as client:
        resp = client.get("http://api.openai.com/v1/models")
    assert resp.status_code == 403


@needs_egress_proxy
def test_egress_denies_non_allowlisted_model_endpoint_https_connect():
    """Mesmo failure mode via túnel HTTPS (CONNECT): o proxy recusa o túnel
    para api.anthropic.com — o cliente vê erro de proxy, nunca um handshake."""
    with httpx.Client(proxy=EGRESS_PROXY_URL, timeout=10.0) as client:
        with pytest.raises(httpx.ProxyError):
            client.get("https://api.anthropic.com/v1/models")


@needs_egress_proxy
def test_egress_allows_only_the_model_gateway_route():
    """Controle positivo: a ÚNICA rota de modelo permitida (model-gateway:4000,
    resolvida DENTRO da rede Docker pelo próprio proxy) funciona através do
    MESMO proxy que negou os endpoints públicos acima — prova que o deny não é
    um proxy quebrado, é política default-deny funcionando."""
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
