"""Ponte de ativação do adapter Teams.

O intake é tipado sobre `dse_contracts.Platform` (enum congelado da fundação:
`slack|github|jira`). Ativar Teams exige, na FUNDAÇÃO (packages/contracts +
migração 0001), adicionar `teams`:
  1. `Platform.teams` no enum de `conversation_event.py` (aditivo, 1 linha).
  2. relaxar os CHECKs `work_items.source` e `identity_links.platform` para
     incluir `'teams'` (ver `activation.sql` — aditivo, aplicar na migração de
     ativação).

Este workstream (WS-A, Fase 4) NÃO edita `packages/*` nem as migrações 0001-0019
(regra de convivência — 4 agentes em paralelo). Logo o adapter é entregue
COMPLETO e testado, porém "wired but not activated": todo o pipeline existe e é
exercido nos testes até o ponto exato do bloqueio de fundação. `is_activated()`
detecta em runtime se a fundação já foi estendida (sem hard-codar o resultado),
para que ligar Teams seja literalmente a mudança de 1 linha no enum + a migração
de ativação — sem tocar neste serviço.
"""
from __future__ import annotations

from dse_contracts import Platform


class TeamsNotActivated(RuntimeError):
    """Levantada quando o pipeline tenta produzir um artefato tipado em
    `Platform.teams` mas a fundação ainda não foi estendida. Mensagem carrega
    os passos de ativação (P6: falha limpa e explicativa em fronteira, nunca
    silenciosa)."""

    def __init__(self) -> None:
        super().__init__(
            "adapter Teams não ativado: adicione `Platform.teams` em "
            "packages/contracts/dse_contracts/conversation_event.py e aplique "
            "services/adapter-teams/activation.sql (relax dos CHECKs de "
            "work_items.source / identity_links.platform). Decisão de negócio/"
            "roadmap Fase 4+ — ver README."
        )


def teams_platform() -> Platform:
    """Retorna `Platform.teams` se a fundação já foi estendida; senão levanta
    `TeamsNotActivated`. Nunca fabrica um valor de enum fora do contrato."""
    try:
        return Platform("teams")
    except ValueError as exc:  # membro ainda não existe no enum congelado
        raise TeamsNotActivated() from exc


def is_activated() -> bool:
    try:
        Platform("teams")
        return True
    except ValueError:
        return False
