"""WSE-E1-T3 — diff final vs PlanArtifact: arquivos tocados vs expected_files,
diff_budget_lines, forbidden_paths. `git diff --numstat` real contra o repo
git de verdade da fixture `git_repo` (nada mockado)."""
from __future__ import annotations

from dse_contracts import PlanArtifact

from dse_validation.l1.plan_compliance import (
    compute_diff_summary,
    diff_budget_finding,
    forbidden_paths_finding,
    plan_compliance_findings,
)


def test_diff_within_budget_and_expected_files_passes(sandbox, feature_branch):
    feature_branch("app.py", "def add(a, b):\n    return a + b  # small tweak\n")
    plan = PlanArtifact(
        work_item_id="wi1", expected_files=["app.py"], diff_budget_lines=50, forbidden_paths=[]
    )
    diff = compute_diff_summary(sandbox, "main")
    finding = diff_budget_finding(diff, plan)
    assert finding.check == "diff_budget"
    assert finding.passed is True


def test_diff_over_budget_fails_and_cites_plan(sandbox, feature_branch, git_repo):
    big_content = "\n".join(f"line_{i} = {i}" for i in range(500))
    feature_branch("app.py", big_content)
    plan = PlanArtifact(
        work_item_id="wi2", expected_files=["app.py"], diff_budget_lines=10, forbidden_paths=[]
    )
    diff = compute_diff_summary(sandbox, "main")
    finding = diff_budget_finding(diff, plan)
    assert finding.passed is False
    assert "diff_budget_lines=10" in finding.detail


def test_diff_touching_unexpected_file_fails(sandbox, feature_branch):
    feature_branch("unexpected_module.py", "x = 1\n")
    plan = PlanArtifact(
        work_item_id="wi3", expected_files=["app.py"], diff_budget_lines=400, forbidden_paths=[]
    )
    diff = compute_diff_summary(sandbox, "main")
    finding = diff_budget_finding(diff, plan)
    assert finding.passed is False
    assert "expected_files" in finding.detail
    assert "unexpected_module.py" in finding.detail


def test_diff_touching_forbidden_path_fails(sandbox, feature_branch):
    feature_branch("migrations/0099_evil.sql", "DROP TABLE audit_log;\n")
    plan = PlanArtifact(work_item_id="wi4", expected_files=[], diff_budget_lines=400)
    diff = compute_diff_summary(sandbox, "main")
    finding = forbidden_paths_finding(diff, plan)
    assert finding.check == "forbidden_paths"
    assert finding.passed is False
    assert "migrations/" in finding.detail


def test_diff_not_touching_forbidden_path_passes(sandbox, feature_branch):
    feature_branch("app.py", "def add(a, b):\n    return a + b  # tweak\n")
    plan = PlanArtifact(work_item_id="wi5", expected_files=["app.py"], diff_budget_lines=400)
    diff = compute_diff_summary(sandbox, "main")
    finding = forbidden_paths_finding(diff, plan)
    assert finding.passed is True


def test_plan_compliance_findings_returns_exactly_two_findings(sandbox, feature_branch):
    feature_branch("app.py", "def add(a, b):\n    return a + b  # tweak2\n")
    plan = PlanArtifact(work_item_id="wi6", expected_files=["app.py"], diff_budget_lines=400)
    findings = plan_compliance_findings(sandbox, plan, "main")
    checks = {f.check for f in findings}
    assert checks == {"diff_budget", "forbidden_paths"}
    assert all(f.passed for f in findings)
