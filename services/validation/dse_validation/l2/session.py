"""WSE-E2-T4 — interface da sessão Reviewer L2 (contexto fresco).

**P3 (no producer approves its own work / reviewer sees no producer history):**
o input da sessão L2 é ESTRUTURALMENTE só `plan` (PlanArtifact) + `diff` (o diff
final, texto). Não existe campo para o histórico/transcript do Coder — não há como
vazá-lo por esta fronteira. A sessão L2 do WS-C recebe exatamente este objeto.

A implementação real da sessão é do WS-C (WSC-E3-T5), exposta como a Activity
`ACTIVITY_RUN_L2_REVIEW`. Como os workstreams constroem em paralelo, aqui há:

  - `L2ReviewSession` (Protocol) — `review(inp) -> L2Verdict`.
  - `FakeL2ReviewSession` — fake determinístico (nenhum LLM), scriptável, usado
    nos testes deste workstream para exercitar a orquestração de verdade sem a
    sessão do WS-C. Explicitamente marcado como fixture (não é produção).
  - `build_l2_session()` — resolve a sessão real do WS-C se ela já estiver
    publicada e importável; caso contrário devolve o fake com um WARNING claro.
"""
from __future__ import annotations

import logging
from collections import deque
from typing import Protocol

from dse_contracts import L2Verdict, PlanArtifact
from pydantic import BaseModel

logger = logging.getLogger("dse_validation.l2")


class L2ReviewInput(BaseModel):
    """Tudo — e SÓ — o que a sessão L2 pode ver (P3). Sem histórico do Coder."""

    work_item_id: str
    tenant_id: str
    plan: PlanArtifact
    diff: str
    iteration: int = 0  # nº do turno L2 dentro do loop de fix-retries (informativo)


class L2ReviewSession(Protocol):
    def review(self, inp: L2ReviewInput) -> L2Verdict: ...


class FakeL2ReviewSession:
    """Fake determinístico para os testes de orquestração do WS-E.

    Modos:
      - `scripted`: uma fila de `L2Verdict` (ou de `(passed, objections)`),
        consumida em ordem a cada `review()` — permite simular
        "reprova, reprova, aprova" para o loop de fix-retries.
      - default: aprova sempre com custo fixo.

    NÃO é produção: nenhuma inteligência real, só devolve o roteiro. A sessão
    real (WS-C) faz a chamada de modelo de contexto fresco de verdade.
    """

    def __init__(
        self,
        scripted: list[L2Verdict] | list[tuple[bool, list[str]]] | None = None,
        *,
        cost_usd: float = 0.02,
    ):
        self._cost = cost_usd
        self._queue: deque = deque(scripted or [])
        self.calls: list[L2ReviewInput] = []

    def review(self, inp: L2ReviewInput) -> L2Verdict:
        # Registra o input recebido para os testes provarem que P3 foi honrado
        # (o objeto não tem campo de histórico do Coder — é estrutural).
        self.calls.append(inp)
        if self._queue:
            item = self._queue.popleft()
            if isinstance(item, L2Verdict):
                return item.model_copy(update={"work_item_id": inp.work_item_id})
            passed, objections = item
            return L2Verdict(
                work_item_id=inp.work_item_id,
                passed=passed,
                objections=list(objections),
                cost_usd=self._cost,
            )
        return L2Verdict(work_item_id=inp.work_item_id, passed=True, objections=[], cost_usd=self._cost)


def build_l2_session() -> L2ReviewSession:
    """Resolve a sessão L2 real do WS-C se importável; senão devolve o fake.

    O WS-C (services/sandbox-runtime) é dono da sessão. Quando publicar um
    builder (ex.: `dse_sandbox_runtime.l2.build_review_session`), este import
    passa a resolvê-lo sem mudar nada aqui. Enquanto ele constrói em paralelo,
    o WARNING deixa explícito que estamos no fake (P8: nunca falha em silêncio)."""
    try:  # pragma: no cover - caminho de integração, exercitado só quando WS-C publica
        from dse_sandbox_runtime.l2 import build_review_session  # type: ignore

        logger.info("dse_validation.l2: usando a sessão Reviewer L2 real do WS-C")
        return build_review_session()
    except Exception:  # ImportError enquanto WS-C não publicou; qualquer erro cai no fake
        logger.warning(
            "dse_validation.l2: sessão Reviewer L2 do WS-C não disponível "
            "(dse_sandbox_runtime.l2) — usando FakeL2ReviewSession (modo local/teste, "
            "NÃO produção)"
        )
        return FakeL2ReviewSession()
