"""WSD-E4-T1 (Fase 3) — failover e degradação INTRA-TIER.

O failover em si é NATIVO do LiteLLM proxy (`router_settings.fallbacks` em
`litellm_config.yaml`): se o deployment primário de um model group falha
(connection refused / 5xx / timeout), o router tenta o fallback declarado —
e somente ele. Este módulo é o lado do CLIENTE dessa história:

  1. `INTRA_TIER_FALLBACKS` — espelho declarativo do mapa de fallbacks do
     proxy. Existe para (a) permitir mintar virtual keys que cobrem o conjunto
     de failover completo (uma key escopada SÓ no primário faz o proxy negar o
     fallback — comportamento correto do backstop server-side, mas então não
     há failover), e (b) permitir o check de política do modelo servido em
     degradação. A consistência entre este espelho e o `litellm_config.yaml`
     é garantida por teste (test_failover_intra_tier.py) — se alguém mudar um
     sem o outro, o CI quebra.

  2. `detect_degradation(...)` — detecção determinística de que a resposta
     veio de um fallback, pelos headers que o LiteLLM devolve
     (`x-litellm-attempted-fallbacks` > 0; `x-litellm-model-api-base`
     identifica o endpoint que de fato serviu).

  3. `audit_degradation(...)` — o audit row de degradação (P8): failover
     nunca é silencioso. `gateway.call_degraded_fallback` com o endpoint
     servidor, os candidatos de fallback e o veredito de política de cada um.

  4. Enforcement de política sobre o modelo servido: se NENHUM dos fallbacks
     declarados do model group é permitido pela política do tenant/stage, a
     resposta degradada é RECUSADA na fronteira (P6) com `policy_denied`
     (kind=fallback_model_not_allowed) — fallback não burla política, igual
     ao reassign (WSD-E4-T2).

Nunca há rota de fallback cruzando tier (NFR-07/P2): o teste negativo
`test_no_fallback_route_crosses_tier` parseia o `litellm_config.yaml` e falha
se qualquer par (primário, fallback) tiver `dse_tier` diferente.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from dse_audit.client import emit as audit_emit
from dse_contracts.gateway_contract import GatewayCallHeaders

from . import policy

_AUDIT_ACTOR = "system:model-gateway"

# Header do LiteLLM: quantos fallbacks foram tentados até esta resposta.
ATTEMPTED_FALLBACKS_HEADER = "x-litellm-attempted-fallbacks"
# Header do LiteLLM: o api_base do deployment que DE FATO serviu a resposta.
MODEL_API_BASE_HEADER = "x-litellm-model-api-base"

# Espelho declarativo de router_settings.fallbacks do litellm_config.yaml
# (ver docstring §1). Sobrescrevível por env var para outros deployments.
_DEFAULT_FALLBACKS = {"eco/echo-model": ["eco/echo-model-b"]}


def intra_tier_fallbacks() -> dict[str, list[str]]:
    raw = os.environ.get("DSE_INTRA_TIER_FALLBACKS")
    if not raw:
        return dict(_DEFAULT_FALLBACKS)
    try:
        parsed = json.loads(raw)
        return {str(k): [str(m) for m in v] for k, v in parsed.items()}
    except (json.JSONDecodeError, TypeError, AttributeError):
        # Config inválida não pode abrir rota de fallback surpresa: cai para
        # o default conhecido (falha fechada para o mapa conhecido).
        return dict(_DEFAULT_FALLBACKS)


def intra_tier_failover_set(model: str) -> list[str]:
    """O conjunto completo de modelos que uma virtual key precisa cobrir para
    o failover intra-tier funcionar: o primário + seus fallbacks declarados.

    É isto que os call sites (sessões do WS-C) devem passar em
    `mint_virtual_key(..., models=intra_tier_failover_set(model))` — uma key
    escopada só no primário faz o proxy (corretamente) negar o fallback."""
    return [model, *intra_tier_fallbacks().get(model, [])]


@dataclass(frozen=True)
class Degradation:
    requested_model: str
    served_api_base: str | None
    attempted_fallbacks: int
    fallback_candidates: list[str]


def detect_degradation(requested_model: str, response_headers) -> Degradation | None:
    """Detecção determinística: o LiteLLM conta os fallbacks tentados no
    header. 0/ausente -> resposta veio do primário -> None."""
    raw = response_headers.get(ATTEMPTED_FALLBACKS_HEADER, "0")
    try:
        attempted = int(raw)
    except (TypeError, ValueError):
        attempted = 0
    if attempted <= 0:
        return None
    return Degradation(
        requested_model=requested_model,
        served_api_base=response_headers.get(MODEL_API_BASE_HEADER),
        attempted_fallbacks=attempted,
        fallback_candidates=intra_tier_fallbacks().get(requested_model, []),
    )


def audit_degradation(
    headers: GatewayCallHeaders, degradation: Degradation
) -> dict[str, bool]:
    """Emite o audit row de degradação (P8 — failover nunca é silencioso) e
    devolve o veredito de política por candidato de fallback (usado pelo
    caller para o enforcement P6 — ver gateway_call.chat_completion)."""
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
