"""S7 (Fase 5) — boundary test do par workflow -> L1 / finalize_pr.

Mesma classe de bug do checkpoint/rebuild: os call sites vivem no workflow
(WS-B) e os modelos de input aqui (WS-E). Os fakes de teste do WS-B eram
lenientes e escondiam divergências — o workflow chamava `run_l1_pipeline` com
`{work_item_id, sandbox_id}` (faltando `sandbox`, `plan`, `tenant_id`,
`base_branch`) e `finalize_pr` sem `summary`; só falhava no DECODE real da
Activity (`Failed decoding arguments`). Estes testes validam o PAYLOAD LITERAL
que o workflow monta contra o modelo pydantic, para a classe de bug falhar no
CI e não em produção. Se o workflow mudar o shape, ATUALIZE os literais aqui.
"""
from __future__ import annotations

from dse_validation.activities import FinalizePrInput, RunL1PipelineInput
from dse_contracts import PlanArtifact, SandboxHandle


def _handle_payload():
    return {
        "sandbox_id": "dse-sandbox-wi-1",
        "work_item_id": "wi-1",
        "tenant_id": "tnt-1",
        "branch": "dse/wi-1",
        "container_id": "abc123",
    }


def test_l1_input_accepts_exact_workflow_payload():
    plan = PlanArtifact(work_item_id="wi-1").model_dump()
    payload = {
        "sandbox": _handle_payload(),  # dict -> SandboxHandle
        "plan": plan,                  # dict -> PlanArtifact
        "tenant_id": "tnt-1",
        "base_branch": "main",
    }
    inp = RunL1PipelineInput(**payload)
    assert inp.sandbox.work_item_id == "wi-1"
    assert inp.base_branch == "main"


def test_finalize_input_accepts_exact_workflow_payload():
    payload = {
        "work_item_id": "wi-1",
        "tenant_id": "tnt-1",
        "sandbox": _handle_payload(),
        "repo": "andre2654/fintex-wallet",
        "base_branch": "main",
        "branch": "dse/wi-1",
        "summary": "DSE: corrige exclusão de transação",
    }
    inp = FinalizePrInput(**payload)
    assert inp.summary.startswith("DSE:")
    assert inp.sandbox.container_id == "abc123"
