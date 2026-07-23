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
import os
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


_L2_PROMPT = """Você é o Reviewer L2 do Fintex DSE — um revisor de CONTEXTO FRESCO (P3):
você vê APENAS o plano e o diff final. Avalie se a mudança implementa o plano
com segurança e qualidade mínimas para abrir um PR (a revisão final é humana).

Responda APENAS com JSON válido:
{{"passed": true|false, "objections": ["objeção específica (arquivo/linha) 1", ...]}}

- passed=true quando o diff cumpre o plano sem problema GRAVE (bug evidente,
  risco de segurança, mudança fora do escopo). Estilo/nit não reprova.
- objections: vazia quando passed; específicas e acionáveis quando não.

## Plano
{plan}

## Diff final
{diff}
"""


class ModelL2ReviewSession:
    """Sessão L2 REAL (achado do disparo real 2026-07-22: o import do builder
    apontava para um módulo inexistente — `dse_sandbox_runtime` — e TODO
    deployment caía no fake que aprova sempre). Mesmo padrão do Planner/Tester:
    1 chamada stage=l2 via gateway (enforcement + ledger no caminho), P3 por
    construção (o prompt só carrega plan+diff). Falha de chamada/parse →
    exceção (a activity retenta; billing/auth viram non-retryable na origem)."""

    def review(self, inp: L2ReviewInput) -> L2Verdict:
        import json as _json
        import os as _os

        from dse_contracts.gateway_contract import GatewayCallHeaders, Stage
        from model_gateway_client.gateway_call import chat_completion
        from model_gateway_client.virtual_keys import mint_virtual_key

        headers = GatewayCallHeaders(
            tenant_id=inp.tenant_id, work_item_id=inp.work_item_id,
            stage=Stage.reviewer, task_class="default", data_class="internal",
        )
        # mint_virtual_key -> str (a key crua); pego pelo shadow-run — o .key
        # de IssuedVirtualKey só existe no retorno rico interno.
        vk_key = mint_virtual_key(inp.tenant_id, inp.work_item_id, Stage.reviewer)
        model = _os.environ.get("DSE_L2_MODEL") or _os.environ.get("DSE_CODER_MODEL", "anthropic/claude")
        prompt = _L2_PROMPT.format(
            plan=inp.plan.model_dump_json()[:4000], diff=(inp.diff or "(diff vazio)")[:20000]
        )
        result = chat_completion(
            headers=headers, virtual_key=vk_key, model=model,
            messages=[{"role": "user", "content": prompt}],
            timeout=120.0, max_tokens=1500, temperature=0,
        )
        text = (result.content or "").strip()
        if text.startswith("```"):
            text = text.strip("`\n")
            text = text[4:] if text.startswith("json") else text
        # raw_decode: o modelo às vezes continua escrevendo APÓS o JSON
        # (pego pelo shadow-run — "Extra data") — usa o 1º objeto e ignora o resto.
        verdict, _ = _json.JSONDecoder().raw_decode(text.strip())
        return L2Verdict(
            work_item_id=inp.work_item_id,
            passed=bool(verdict.get("passed")),
            objections=[str(o) for o in (verdict.get("objections") or [])][:10],
            cost_usd=float(result.cost_usd or 0.0),
        )


def build_l2_session() -> L2ReviewSession:
    """Sessão L2 por CONFIG (P1): substrato real configurado → ModelL2ReviewSession
    (modelo via gateway); senão o fake explícito (modo local/teste, com WARNING —
    P8: nunca em silêncio)."""
    if os.environ.get("DSE_CODER_SUBSTRATE", "fake").strip().lower() != "fake":
        logger.info("dse_validation.l2: sessão Reviewer L2 REAL (modelo via gateway)")
        return ModelL2ReviewSession()
    logger.warning(
        "dse_validation.l2: DSE_CODER_SUBSTRATE=fake — usando FakeL2ReviewSession "
        "(modo local/teste, NÃO produção)"
    )
    return FakeL2ReviewSession()
