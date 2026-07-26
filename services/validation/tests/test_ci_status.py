"""WSE-E4-T9a — minimal consumption of PR status checks. `FakeGitHubClient`
supplies the check-runs (no real GitHub App in this session); persistence to
`wse_ci_status` is REAL Postgres."""
from __future__ import annotations

from dse_validation import db
from dse_validation.github.ci_status import aggregate_check_runs, consume_ci_status_core
from dse_validation.github.client import FakeGitHubClient


def test_aggregate_no_check_runs_is_pending():
    assert aggregate_check_runs([]) == "pending"


def test_aggregate_all_success_is_green():
    runs = [
        {"name": "lint", "status": "completed", "conclusion": "success"},
        {"name": "tests", "status": "completed", "conclusion": "success"},
    ]
    assert aggregate_check_runs(runs) == "green"


def test_aggregate_any_failure_is_red():
    runs = [
        {"name": "lint", "status": "completed", "conclusion": "success"},
        {"name": "tests", "status": "completed", "conclusion": "failure"},
    ]
    assert aggregate_check_runs(runs) == "red"


def test_aggregate_still_running_is_pending_even_if_others_passed():
    runs = [
        {"name": "lint", "status": "completed", "conclusion": "success"},
        {"name": "tests", "status": "in_progress", "conclusion": None},
    ]
    assert aggregate_check_runs(runs) == "pending"


def test_consume_ci_status_core_persists_to_real_postgres(work_item_id, tenant_id):
    github = FakeGitHubClient()
    github.set_check_runs(
        "acme/repo",
        "abc123",
        [{"name": "build", "status": "completed", "conclusion": "success"}],
    )
    result = consume_ci_status_core(
        github_client=github,
        work_item_id=work_item_id,
        tenant_id=tenant_id,
        repo="acme/repo",
        pr_number=55,
        ref="abc123",
    )
    assert result.status == "green"
    assert result.pr_number == 55

    row = db.get_ci_status(work_item_id)
    assert row is not None
    assert row["status"] == "green"
    assert row["pr_number"] == 55


def test_consume_ci_status_core_red_on_failed_check(work_item_id, tenant_id):
    github = FakeGitHubClient()
    github.set_check_runs(
        "acme/repo",
        "def456",
        [{"name": "tests", "status": "completed", "conclusion": "failure"}],
    )
    result = consume_ci_status_core(
        github_client=github,
        work_item_id=work_item_id,
        tenant_id=tenant_id,
        repo="acme/repo",
        pr_number=56,
        ref="def456",
    )
    assert result.status == "red"
