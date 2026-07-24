"""Classes estruturadas de falha na fronteira Activity→workflow (Fase 2, plano 09).

Antes: o workflow classificava recusa fail-closed por SUBSTRING da mensagem
(`_FAIL_CLOSED_MARKERS`) — um erro transitório com a palavra errada matava a
tarefa. Agora: quem LANÇA a falha declara a classe no `type` do
`ApplicationError` (vocabulário fechado abaixo), e o workflow decide pelo
type — a mensagem volta a ser só diagnóstico humano.

Regras:
  - `failure_type(FailureClass.x)` produz o type canônico ("dse.failure.x").
  - `parse_failure_type` reconhece TAMBÉM os types legados já gravados em
    histórias do Temporal (ProviderBillingError, EgressFailClosed) — replay
    de workflow em voo nunca reclassifica errado.
  - INFRA_TRANSIENT existe para rotular explicitamente o retryable; o
    mecanismo continua sendo o do Temporal (erro retryable + teto de
    wall-clock) — nunca `non_retryable`.
"""
from __future__ import annotations

from enum import Enum

FAILURE_TYPE_PREFIX = "dse.failure."


class FailureClass(str, Enum):
    policy_fail_closed = "policy_fail_closed"  # egress down, vk expirada, kill switch, policy denied
    budget_denied = "budget_denied"            # teto de budget na admissão/fronteira
    provider_billing = "provider_billing"      # créditos/billing/auth do provider esgotados
    infra_transient = "infra_transient"        # oscilação retryable (gateway blip etc.)


# types já gravados em histórias antes do vocabulário canônico — o parse os
# reconhece para sempre (replay-safety); NUNCA remova entradas daqui.
_LEGACY_TYPE_MAP: dict[str, FailureClass] = {
    "ProviderBillingError": FailureClass.provider_billing,
    "EgressFailClosed": FailureClass.policy_fail_closed,
}

# Classes que o workflow converte em falha limpa fail-closed (P6) — as demais
# seguem o caminho de exaustão/retry normal.
FAIL_CLOSED_CLASSES = frozenset(
    {FailureClass.policy_fail_closed, FailureClass.budget_denied, FailureClass.provider_billing}
)


def failure_type(kind: FailureClass) -> str:
    return f"{FAILURE_TYPE_PREFIX}{kind.value}"


def parse_failure_type(type_name: str | None) -> FailureClass | None:
    """Type do ApplicationError → FailureClass (canônico ou legado); None para
    qualquer type fora do vocabulário (erro comum, exceção de runtime etc.)."""
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
