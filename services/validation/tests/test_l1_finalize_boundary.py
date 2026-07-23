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

from dse_validation.activities import (
    ConsumeCiStatusInput,
    FinalizePrInput,
    RunL1PipelineInput,
)
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
        # back-link da issue ("Closes #N") + evidência L1 no corpo do PR.
        "issue_ref": {"issue_number": 2},
        "evidence_url": "L1 green (test ✓, secret_scan ✓)",
    }
    inp = FinalizePrInput(**payload)
    assert inp.summary.startswith("DSE:")
    assert inp.sandbox.container_id == "abc123"
    assert inp.issue_ref == {"issue_number": 2}
    assert "L1 green" in inp.evidence_url


def test_finalize_input_tolerates_absent_issue_and_evidence():
    # work item sem issue de origem (ex.: origem Slack/Jira sem numero) —
    # o workflow manda issue_ref=None e evidence vazio; o finalizer usa os
    # fallbacks "(sem ...)" no corpo.
    inp = FinalizePrInput(
        work_item_id="wi-1", tenant_id="tnt-1", sandbox=_handle_payload(),
        repo="a/b", base_branch="main", branch="dse/wi-1", summary="s",
        issue_ref=None, evidence_url="",
    )
    assert inp.issue_ref is None and inp.evidence_url == ""


def test_consume_ci_input_accepts_exact_workflow_payload():
    # Auditoria pós-S7: o call site do loop de review mandava só
    # {work_item_id, pr_number} — faltavam tenant_id/repo/ref (obrigatorios).
    # Todo ciclo de review com CI quebrava no decode. Literal corrigido:
    inp = ConsumeCiStatusInput(
        **{"work_item_id": "wi-1", "tenant_id": "tnt-1",
           "repo": "andre2654/fintex-wallet", "ref": "dse/wi-1", "pr_number": 6}
    )
    assert inp.ref == "dse/wi-1"
    assert inp.pr_number == 6
