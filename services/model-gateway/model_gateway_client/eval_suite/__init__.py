"""Suite de avaliação Tier-2 do model-gateway (WSD-E5-T1).

DONO (owner nomeado — gap 6 do plano): WS-D (model-gateway). Contato de
manutenção: o time do model-gateway é responsável por manter `cases.yaml`
atualizado conforme novos modelos entram no `litellm_config.yaml`.

O que é: um harness pequeno, REAL e rodável que dispara um conjunto de prompts
de referência contra os modelos configurados no gateway e reporta pass/fail +
custo + latência por caso. NÃO é o Tier-2 air-gapped serving completo (Fase 4)
— é a estrutura mínima de eval pedida agora, para que "trocar de modelo" ou
"subir uma versão do LiteLLM" tenha um gate objetivo antes de promover.

Rodar:
    python -m model_gateway_client.eval_suite            # todos os modelos alcançáveis
    python -m model_gateway_client.eval_suite --model eco/echo-model

Modelos não alcançáveis (ex.: os aliases `bedrock/*` sem AWS nesta sessão) são
reportados como SKIPPED, não como falha — o harness distingue "modelo errou a
asserção" de "modelo indisponível na infra atual".
"""
from __future__ import annotations

from .runner import EvalCaseResult, EvalReport, run_suite

__all__ = ["run_suite", "EvalReport", "EvalCaseResult"]
