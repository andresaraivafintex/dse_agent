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
    ConsumeCiStatusInput,
    FinalizePrInput,
    GateStatus,
    L2Verdict,
    L1Finding,
    L1Result,
    MergedByHumanSignal,
    PersistWorkItemStateInput,
    PlanArtifact,
    RunDemoEvidenceInput,
    RunL1PipelineInput,
    RunL2ReviewInput,
    RunPlannerTurnInput,
    RunTesterTurnInput,
    RunVisualDiffInput,
    TesterTurnResult as _TesterTurnResult,
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
    tr = _TesterTurnResult(
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


# ---------------------------------------------------------------------------
# Fase 3 — payloads EXATOS dos call sites do pipeline de evidencia do workflow
# (services/orchestrator/src/dse_orchestrator/workflows.py::
# _run_evidence_pipeline). Regra do arquivo: call site e teste de boundary
# mudam JUNTOS, no mesmo conjunto de mudancas (WS-B).
# ---------------------------------------------------------------------------
WSB_TRIGGER_PREVIEW_PAYLOAD = {
    "work_item_id": "wi_x",
    "tenant_id": "tenant_dev",
    "repo": "acme/repo",
    "pr_number": 1000,
    "files_changed": ["frontend/App.tsx", "api/handler.py"],
}

WSB_DEMO_EVIDENCE_PAYLOAD = {
    "work_item_id": "wi_x",
    "tenant_id": "tenant_dev",
    "base_url": "http://preview-wi_x.local",  # PreviewRef.url do trigger_preview
}

WSB_VISUAL_DIFF_PAYLOAD = {
    "work_item_id": "wi_x",
    "tenant_id": "tenant_dev",
    "base_screenshot_key": None,  # None no 1o run -> baseline (visual_baseline_key depois)
    "candidate_screenshot_path": "demos/wi_x/screenshot.png",  # convencao ADR-27
}


def test_trigger_preview_accepts_exact_wsb_payload():
    inp = TriggerPreviewInput(**WSB_TRIGGER_PREVIEW_PAYLOAD)
    # WS-B NAO envia ui_path_globs — a politica de paths e default do contrato
    # (dono WS-E pode sobrepor); files_changed vem do CoderTurnResult.
    assert inp.ui_path_globs
    assert inp.files_changed == ["frontend/App.tsx", "api/handler.py"]


def test_demo_evidence_accepts_exact_wsb_payload():
    inp = RunDemoEvidenceInput(**WSB_DEMO_EVIDENCE_PAYLOAD)
    assert inp.base_url == "http://preview-wi_x.local"
    # WS-B nao envia demo_dir/timeout_s/sandbox — defaults do dono (WS-E)
    assert inp.demo_dir == "" and inp.timeout_s == 120 and inp.sandbox is None


def test_visual_diff_accepts_exact_wsb_payload():
    inp = RunVisualDiffInput(**WSB_VISUAL_DIFF_PAYLOAD)
    assert inp.base_screenshot_key is None
    assert inp.candidate_screenshot_path == "demos/wi_x/screenshot.png"
    assert inp.threshold_pct == 0.1  # default do contrato — WS-B nao sobrepoe


# ---------------------------------------------------------------------------
# Fase 4 — payloads EXATOS dos call sites de merge-base e promoção de skill.
# ---------------------------------------------------------------------------
from dse_contracts import (  # noqa: E402
    EvalSkillCandidateInput,
    PromoteSkillInput,
    UpdateBaseBranchInput,
)

WSB_UPDATE_BASE_PAYLOAD = {
    "work_item_id": "wi_x",
    "tenant_id": "tenant_dev",
    "repo": "acme/repo",
    "branch": "dse/wi_x",
    "base_branch": "main",
    "first_human_review_done": True,
}


def test_update_base_branch_accepts_exact_wsb_payload():
    inp = UpdateBaseBranchInput(**WSB_UPDATE_BASE_PAYLOAD)
    # default seguro: depois do 1o review, NUNCA rebase (só merge-base)
    assert inp.first_human_review_done is True


def test_update_base_branch_default_is_review_done_never_rebase():
    """Invariante de segurança: se o chamador OMITIR first_human_review_done,
    o default é True — ou seja, o caminho conservador (nunca rebase). Um
    esquecimento no call site NÃO pode abrir a porta para force-push."""
    inp = UpdateBaseBranchInput(
        work_item_id="w", tenant_id="t", repo="r", branch="b", base_branch="main"
    )
    assert inp.first_human_review_done is True


def test_promote_skill_requires_approver_for_approved_active():
    """P1/P3 no contrato: o model aceita a intenção, mas a Activity (WS-C) DEVE
    recusar to_status approved/active sem approver. Aqui garantimos que o campo
    approver existe e é opcional no wire (a obrigatoriedade é enforcement da
    Activity, testado no WS-C) — o contrato não pode ESCONDER o approver."""
    inp = PromoteSkillInput(tenant_id="t", skill_key="s", version=1, to_status="canary")
    assert inp.approver is None  # canary pode não ter approver; approved/active não (WS-C valida)
    inp2 = PromoteSkillInput(tenant_id="t", skill_key="s", version=1,
                             to_status="active", approver="usr_alice")
    assert inp2.approver == "usr_alice"


def test_eval_skill_candidate_shape():
    inp = EvalSkillCandidateInput(tenant_id="t", skill_key="s", candidate_version=3)
    assert inp.candidate_version == 3


def test_gate_status_is_additive_and_only_pass_is_true():
    legacy = L1Finding(check="lint", passed=True)
    assert legacy.status == GateStatus.PASS

    missing = L1Finding(check="build", status=GateStatus.NOT_CONFIGURED)
    assert missing.passed is False
    result = L1Result(work_item_id="wi_x", passed=False, findings=[missing])
    assert result.status == GateStatus.NOT_CONFIGURED

    with pytest.raises(ValidationError):
        L1Finding(check="test", passed=True, status=GateStatus.SKIPPED)


def test_historical_activity_payloads_decode_with_server_side_gaps():
    # Shapes observados em histories antigos: campos novos nao podem impedir
    # o decode; o owner resolve sandbox/plano/repo/ref pelo work_item_id.
    old_l1 = RunL1PipelineInput(work_item_id="wi_x", sandbox_id="sbx-old")
    assert old_l1.base_sha == "" and old_l1.head_sha == ""
    assert old_l1.sandbox is None and old_l1.plan is None

    old_finalize = FinalizePrInput(work_item_id="wi_x", sandbox_id="sbx-old")
    assert old_finalize.repo == "" and old_finalize.summary == ""

    old_ci = ConsumeCiStatusInput(work_item_id="wi_x", pr_number=7)
    assert old_ci.tenant_id == "" and old_ci.repo == "" and old_ci.ref == ""

    old_state = PersistWorkItemStateInput(
        work_item_id="wi_x", status="validating", pr_number=7
    )
    assert old_state.base_sha is None and old_state.plan is None


def test_sha_boundaries_roundtrip_in_contracts():
    inp = RunL1PipelineInput(
        work_item_id="wi_x", tenant_id="t", base_sha="base123", head_sha="head456"
    )
    assert (inp.base_sha, inp.head_sha) == ("base123", "head456")
    verdict = L2Verdict(
        work_item_id="wi_x", passed=True, base_sha=inp.base_sha, head_sha=inp.head_sha
    )
    assert verdict.status == GateStatus.PASS and verdict.head_sha == "head456"


def test_merge_signal_requires_human_and_positive_pr():
    assert MergedByHumanSignal(merged_by="usr_alice", pr_number=42).pr_number == 42
    with pytest.raises(ValidationError):
        MergedByHumanSignal(merged_by="", pr_number=42)
    with pytest.raises(ValidationError):
        MergedByHumanSignal(merged_by="system:orchestrator", pr_number=42)
