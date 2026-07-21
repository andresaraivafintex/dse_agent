"""Testes de regressão de BOUNDARY (adendo 02 §2.3, gate de entrada da Fase 3).

Nas Fases 1-2, 14 bugs de integração nasceram do mesmo padrão: o payload que o
workflow (WS-B) envia derivou dos campos que a Activity (WS-C/WS-E) declara,
e os fakes lenientes dos testes de cada lado (aceitam qualquer dict) nunca
exercitaram o decode real. Estes testes validam os models do contrato contra
os PAYLOADS EXATOS que `services/orchestrator/src/dse_orchestrator/workflows.py`
constrói — se o WS-B mudar o payload OU o model mudar um campo, quebra AQUI,
na fundação, antes de quebrar no wire.

Regra de manutenção: ao mudar um call site no workflow, atualize o payload
correspondente aqui NO MESMO PR (e vice-versa). Payloads copiados literalmente
dos call sites — não "equivalentes".
"""
import pytest
from pydantic import ValidationError

from dse_contracts import (
    CoderTurnResult,
    L2Verdict,
    PlanArtifact,
    RunDemoEvidenceInput,
    RunL2ReviewInput,
    RunPlannerTurnInput,
    RunTesterTurnInput,
    TesterTurnResult,
    TriggerPreviewInput,
)


# Payloads copiados dos call sites reais do workflow (workflows.py).
WSB_PLANNER_PAYLOAD = {
    "work_item_id": "wi_x",
    "tenant_id": "tenant_dev",
    "repo": "acme/repo",
    "base_branch": "main",
    "instructions": ["crit A", "crit B"],
    "model_override": None,
}

WSB_TESTER_PAYLOAD = {
    "sandbox_id": "dse-sandbox-wi_x",
    "work_item_id": "wi_x",
    "tenant_id": "tenant_dev",
    "plan": {"work_item_id": "wi_x", "test_plan": "run pytest -q"},
    "model_override": None,
    "runtime_override": None,
}

WSB_L2_PAYLOAD = {
    "work_item_id": "wi_x",
    "tenant_id": "tenant_dev",
    "plan": {"work_item_id": "wi_x"},
    "diff": "M app.py | +3 -1",
}


def test_planner_input_accepts_exact_wsb_payload():
    inp = RunPlannerTurnInput(**WSB_PLANNER_PAYLOAD)
    # reconciliação: instructions (lista) -> instruction; base_branch -> branch
    assert inp.instruction == "crit A crit B"
    assert inp.branch == "main"


def test_tester_input_accepts_exact_wsb_payload():
    inp = RunTesterTurnInput(**WSB_TESTER_PAYLOAD)
    assert inp.instruction == "run pytest -q"
    assert inp.sandbox_id == "dse-sandbox-wi_x"


def test_tester_result_decodes_as_coder_turn_result():
    # O workflow declara CoderTurnResult como tipo de retorno do Tester — o
    # superset TesterTurnResult tem que decodificar limpo nesse tipo.
    tr = TesterTurnResult(
        sandbox_id="s", test_files=["tests/test_x.py"], tests_ran=True,
        tests_passed=True, returncode=0, cost_usd=0.02,
    )
    cr = CoderTurnResult(**tr.model_dump())
    assert cr.files_changed == ["tests/test_x.py"]
    assert cr.diff_summary  # nunca vazio


def test_l2_input_accepts_exact_wsb_payload():
    inp = RunL2ReviewInput(**WSB_L2_PAYLOAD)
    assert inp.diff == "M app.py | +3 -1"
    assert isinstance(inp.plan, PlanArtifact)


def test_l2_input_forbids_coder_history_structurally():
    """P3 endurecido: extra='forbid' faz o decode FALHAR se qualquer campo
    além dos declarados for enviado — histórico do Coder não tem por onde
    entrar, nem por acidente de payload."""
    for forbidden_field in ("instructions", "clarification_notes", "coder_history",
                            "transcript", "sandbox_id", "diff_summary", "files_changed"):
        with pytest.raises(ValidationError):
            RunL2ReviewInput(**{**WSB_L2_PAYLOAD, forbidden_field: "x"})


def test_l2_verdict_roundtrip():
    v = L2Verdict(work_item_id="wi_x", passed=False, objections=["app.py:12 sem teste"])
    assert L2Verdict(**v.model_dump()) == v


def test_preview_skip_decision_is_deterministic_by_paths():
    """FR-20: a decisão UI-touching é paths-filter puro. O model carrega os
    globs; a decisão em si vive no WS-E, mas o contrato garante que os campos
    necessários (files_changed + globs) atravessam a fronteira."""
    inp = TriggerPreviewInput(
        work_item_id="wi_x", tenant_id="t", repo="acme/repo", pr_number=7,
        files_changed=["api/handler.py", "README.md"],
    )
    assert inp.ui_path_globs  # default não-vazio
    # payload de PR backend-only é representável sem nenhum campo extra
    assert all(not f.endswith((".tsx", ".css")) for f in inp.files_changed)


def test_demo_evidence_input_defaults():
    inp = RunDemoEvidenceInput(work_item_id="wi_x", tenant_id="t")
    assert inp.timeout_s == 120
    assert inp.demo_dir == ""  # derivado no dono: demos/<work_item_id>/
