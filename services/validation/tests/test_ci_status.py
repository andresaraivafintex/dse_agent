"""WSE-E4-T9a — minimal consumption of PR status checks. `FakeGitHubClient`
supplies the check-runs (no real GitHub App in this session); persistence to
`wse_ci_status` is REAL Postgres."""
from __future__ import annotations

from dse_validation import db
from dse_validation.github.ci_status import (
    CI_NO_CI,
    aggregate_check_runs,
    aggregate_ci,
    aggregate_combined_status,
    consume_ci_status_core,
)
from dse_validation.github.client import FakeGitHubClient


def test_aggregate_no_check_runs_is_no_ci_not_pending():
    """The bug this vocabulary exists for: an empty array used to read as
    "still running", so a repo that will never report anything waited forever
    and no work item ever finished."""
    assert aggregate_check_runs([]) == CI_NO_CI


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


# --------------------------------------------------------------------------
# The legacy commit-status source, and how the two combine.
# --------------------------------------------------------------------------


def test_combined_status_keys_off_the_array_never_off_state():
    """GitHub answers `state: "pending"` for a commit nothing has reported on.
    Trusting `state` would recreate the original bug on the legacy side."""
    assert aggregate_combined_status({"state": "pending", "statuses": [], "total_count": 0}) == CI_NO_CI
    assert aggregate_combined_status(None) == CI_NO_CI


def test_combined_status_maps_real_states():
    ok = {"state": "success", "statuses": [{"context": "ci/jenkins"}]}
    bad = {"state": "failure", "statuses": [{"context": "ci/jenkins"}]}
    running = {"state": "pending", "statuses": [{"context": "ci/jenkins"}]}
    assert aggregate_combined_status(ok) == "green"
    assert aggregate_combined_status(bad) == "red"
    assert aggregate_combined_status(running) == "pending"


def test_no_ci_requires_both_sources_to_be_silent():
    """A repo whose CI reports only through legacy statuses must NOT be read as
    having no CI — that would send an unverified change to a human as if
    nothing were expected."""
    legacy_only = {"state": "success", "statuses": [{"context": "ci/jenkins"}]}
    assert aggregate_ci([], legacy_only) == "green"
    assert aggregate_ci([], {"state": "pending", "statuses": [], "total_count": 0}) == CI_NO_CI
    assert aggregate_ci([], None) == CI_NO_CI


def test_red_beats_pending_and_pending_beats_green_across_sources():
    green_runs = [{"name": "lint", "status": "completed", "conclusion": "success"}]
    running_runs = [{"name": "lint", "status": "in_progress"}]
    red_legacy = {"state": "failure", "statuses": [{"context": "ci/jenkins"}]}
    running_legacy = {"state": "pending", "statuses": [{"context": "ci/jenkins"}]}
    assert aggregate_ci(green_runs, red_legacy) == "red"
    assert aggregate_ci(running_runs, red_legacy) == "red"
    assert aggregate_ci(green_runs, running_legacy) == "pending"


def test_consume_records_no_ci_when_the_repo_reports_nothing(work_item_id, tenant_id):
    """End to end through the persistence layer: with neither source reporting,
    the status the workflow reads — and the row it persists — must be `no_ci`.
    This is the exact shape of andre2654/fintex-wallet, where 8 PRs sat in
    `ci_pending` forever."""
    github = FakeGitHubClient()
    result = consume_ci_status_core(
        github_client=github,
        work_item_id=work_item_id,
        tenant_id=tenant_id,
        repo="acme/repo",
        pr_number=42,
        ref="sha-none",
    )
    assert result.status == CI_NO_CI

    row = db.get_ci_status(work_item_id)
    assert row is not None
    assert row["status"] == CI_NO_CI
    # the detail records that BOTH sources were consulted and both were silent
    assert row["detail"]["check_runs"] == []
    assert row["detail"]["combined_status"] == {"state": "pending", "contexts": []}


def test_legacy_statuses_alone_are_enough_to_avoid_no_ci(work_item_id, tenant_id):
    """A repo reporting only through commit statuses must be read as having CI."""
    github = FakeGitHubClient()
    github.set_combined_status(
        "acme/repo", "sha-legacy",
        {"state": "success", "statuses": [{"context": "ci/jenkins"}], "total_count": 1},
    )
    result = consume_ci_status_core(
        github_client=github,
        work_item_id=work_item_id,
        tenant_id=tenant_id,
        repo="acme/repo",
        pr_number=43,
        ref="sha-legacy",
    )
    assert result.status == "green"
