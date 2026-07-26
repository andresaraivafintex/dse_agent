"""BOUNDARY regression tests (adendo 02 §2.3, Phase 3 entry gate).

In Phases 1-2, 14 integration bugs came from the same pattern: the payload the
workflow (WS-B) sends drifted away from the fields the Activity (WS-C/WS-E)
declares, and the lenient fakes in each side's tests (they accept any dict)
never exercised the real decode. These tests validate the contract models
against the EXACT PAYLOADS that
`services/orchestrator/src/dse_orchestrator/workflows.py` builds — if WS-B
changes the payload OR the model changes a field, it breaks HERE, in the
foundation, before breaking on the wire.

Maintenance rule: when changing a call site in the workflow, update the
corresponding payload here IN THE SAME PR (and vice versa). Payloads are copied
literally from the call sites — not "equivalent" ones.
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


# Payloads copied from the real call sites of the workflow (workflows.py).
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
    # reconciliation: instructions (list) -> instruction; base_branch -> branch
    assert inp.instruction == "crit A crit B"
    assert inp.branch == "main"


def test_tester_input_accepts_exact_wsb_payload():
    inp = RunTesterTurnInput(**WSB_TESTER_PAYLOAD)
    assert inp.instruction == "run pytest -q"
    assert inp.sandbox_id == "dse-sandbox-wi_x"


def test_tester_result_decodes_as_coder_turn_result():
    # The workflow declares CoderTurnResult as the Tester's return type — the
    # TesterTurnResult superset has to decode cleanly into that type.
    tr = _TesterTurnResult(
        sandbox_id="s", test_files=["tests/test_x.py"], tests_ran=True,
        tests_passed=True, returncode=0, cost_usd=0.02,
    )
    cr = CoderTurnResult(**tr.model_dump())
    assert cr.files_changed == ["tests/test_x.py"]
    assert cr.diff_summary  # never empty


def test_l2_input_accepts_exact_wsb_payload():
    inp = RunL2ReviewInput(**WSB_L2_PAYLOAD)
    assert inp.diff == "M app.py | +3 -1"
    assert isinstance(inp.plan, PlanArtifact)


def test_l2_input_forbids_coder_history_structurally():
    """Hardened P3: extra='forbid' makes the decode FAIL if any field beyond
    the declared ones is sent — the Coder history has no way in, not even by
    payload accident."""
    for forbidden_field in ("instructions", "clarification_notes", "coder_history",
                            "transcript", "sandbox_id", "diff_summary", "files_changed"):
        with pytest.raises(ValidationError):
            RunL2ReviewInput(**{**WSB_L2_PAYLOAD, forbidden_field: "x"})


def test_l2_verdict_roundtrip():
    v = L2Verdict(work_item_id="wi_x", passed=False, objections=["app.py:12 sem teste"])
    assert L2Verdict(**v.model_dump()) == v


def test_preview_skip_decision_is_deterministic_by_paths():
    """FR-20: the UI-touching decision is pure paths-filter. The model carries
    the globs; the decision itself lives in WS-E, but the contract guarantees
    the required fields (files_changed + globs) cross the boundary."""
    inp = TriggerPreviewInput(
        work_item_id="wi_x", tenant_id="t", repo="acme/repo", pr_number=7,
        files_changed=["api/handler.py", "README.md"],
    )
    assert inp.ui_path_globs  # non-empty default
    # a backend-only PR payload is representable without any extra field
    assert all(not f.endswith((".tsx", ".css")) for f in inp.files_changed)


def test_demo_evidence_input_defaults():
    inp = RunDemoEvidenceInput(work_item_id="wi_x", tenant_id="t")
    assert inp.timeout_s == 120
    assert inp.demo_dir == ""  # derived at the owner: demos/<work_item_id>/


# ---------------------------------------------------------------------------
# Phase 3 — EXACT payloads of the call sites of the workflow's evidence
# pipeline (services/orchestrator/src/dse_orchestrator/workflows.py::
# _run_evidence_pipeline). Rule of this file: call site and boundary test
# change TOGETHER, in the same change set (WS-B).
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
    "base_url": "http://preview-wi_x.local",  # PreviewRef.url from trigger_preview
}

WSB_VISUAL_DIFF_PAYLOAD = {
    "work_item_id": "wi_x",
    "tenant_id": "tenant_dev",
    "base_screenshot_key": None,  # None on the 1st run -> baseline (visual_baseline_key afterwards)
    "candidate_screenshot_path": "demos/wi_x/screenshot.png",  # ADR-27 convention
}


def test_trigger_preview_accepts_exact_wsb_payload():
    inp = TriggerPreviewInput(**WSB_TRIGGER_PREVIEW_PAYLOAD)
    # WS-B does NOT send ui_path_globs — the paths policy is a contract default
    # (owner WS-E may override it); files_changed comes from CoderTurnResult.
    assert inp.ui_path_globs
    assert inp.files_changed == ["frontend/App.tsx", "api/handler.py"]


def test_demo_evidence_accepts_exact_wsb_payload():
    inp = RunDemoEvidenceInput(**WSB_DEMO_EVIDENCE_PAYLOAD)
    assert inp.base_url == "http://preview-wi_x.local"
    # WS-B does not send demo_dir/timeout_s/sandbox — owner defaults (WS-E)
    assert inp.demo_dir == "" and inp.timeout_s == 120 and inp.sandbox is None


def test_visual_diff_accepts_exact_wsb_payload():
    inp = RunVisualDiffInput(**WSB_VISUAL_DIFF_PAYLOAD)
    assert inp.base_screenshot_key is None
    assert inp.candidate_screenshot_path == "demos/wi_x/screenshot.png"
    assert inp.threshold_pct == 0.1  # contract default — WS-B does not override


# ---------------------------------------------------------------------------
# Phase 4 — EXACT payloads of the merge-base and skill-promotion call sites.
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
    # safe default: after the 1st review, NEVER rebase (only merge-base)
    assert inp.first_human_review_done is True


def test_update_base_branch_default_is_review_done_never_rebase():
    """Safety invariant: if the caller OMITS first_human_review_done, the
    default is True — that is, the conservative path (never rebase). An
    oversight at the call site must NOT open the door to force-push."""
    inp = UpdateBaseBranchInput(
        work_item_id="w", tenant_id="t", repo="r", branch="b", base_branch="main"
    )
    assert inp.first_human_review_done is True


def test_promote_skill_requires_approver_for_approved_active():
    """P1/P3 in the contract: the model accepts the intent, but the Activity
    (WS-C) MUST refuse to_status approved/active without an approver. Here we
    guarantee the approver field exists and is optional on the wire (making it
    mandatory is Activity enforcement, tested in WS-C) — the contract must not
    HIDE the approver."""
    inp = PromoteSkillInput(tenant_id="t", skill_key="s", version=1, to_status="canary")
    assert inp.approver is None  # canary may have no approver; approved/active may not (WS-C validates)
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
    # Shapes observed in old histories: new fields must not prevent the decode;
    # the owner resolves sandbox/plan/repo/ref from the work_item_id.
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
