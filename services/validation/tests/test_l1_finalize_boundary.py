"""S7 (Phase 5) — boundary test for the workflow -> L1 / finalize_pr pair.

Same bug class as checkpoint/rebuild: the call sites live in the workflow (WS-B)
and the input models here (WS-E). WS-B's test fakes were lenient and hid the
divergence — the workflow called `run_l1_pipeline` with
`{work_item_id, sandbox_id}` (missing `sandbox`, `plan`, `tenant_id`,
`base_branch`) and `finalize_pr` without `summary`; it only failed at the real
Activity DECODE (`Failed decoding arguments`). These tests validate the LITERAL
PAYLOAD the workflow assembles against the pydantic model, so that this bug class
fails in CI and not in production. If the workflow changes the shape, UPDATE the
literals here.
"""
from __future__ import annotations

from dse_validation.activities import (
    ConsumeCiStatusInput,
    FinalizePrInput,
    RunL1PipelineInput,
)
from dse_contracts import PlanArtifact


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
        "summary": "DSE: fix transaction deletion",
        # issue back-link ("Closes #N") + L1 evidence in the PR body.
        "issue_ref": {"issue_number": 2},
        "evidence_url": "L1 green (test ✓, secret_scan ✓)",
    }
    inp = FinalizePrInput(**payload)
    assert inp.summary.startswith("DSE:")
    assert inp.sandbox.container_id == "abc123"
    assert inp.issue_ref == {"issue_number": 2}
    assert "L1 green" in inp.evidence_url


def test_finalize_input_tolerates_absent_issue_and_evidence():
    # work item with no originating issue (e.g. Slack/Jira origin with no number) —
    # the workflow sends issue_ref=None and empty evidence; the finalizer uses the
    # "(sem ...)" fallbacks in the body.
    inp = FinalizePrInput(
        work_item_id="wi-1", tenant_id="tnt-1", sandbox=_handle_payload(),
        repo="a/b", base_branch="main", branch="dse/wi-1", summary="s",
        issue_ref=None, evidence_url="",
    )
    assert inp.issue_ref is None and inp.evidence_url == ""


def test_consume_ci_input_accepts_exact_workflow_payload():
    # Post-S7 audit: the review loop's call site sent only
    # {work_item_id, pr_number} — tenant_id/repo/ref (required) were missing.
    # Every review cycle involving CI broke at decode. Corrected literal:
    inp = ConsumeCiStatusInput(
        **{"work_item_id": "wi-1", "tenant_id": "tnt-1",
           "repo": "andre2654/fintex-wallet", "ref": "dse/wi-1", "pr_number": 6}
    )
    assert inp.ref == "dse/wi-1"
    assert inp.pr_number == 6
