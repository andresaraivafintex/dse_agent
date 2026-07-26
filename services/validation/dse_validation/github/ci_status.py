"""WSE-E4-T9a — minimal consumption of PR status checks: reads GitHub's
check-runs for the branch's tip commit and aggregates them into a coarse signal
(`pending|green|red`) for the WS-B workflow to decide what to do (P1: the
workflow decides, this function only delivers the result). No previews, no
selective re-run (Phase 3) — just reading + deterministic aggregation.
"""
from __future__ import annotations

from dse_contracts import CiStatusResult

from dse_validation import db
from dse_validation.github.client import GitHubClient

try:
    from dse_audit import emit as audit_emit
except ImportError:  # pragma: no cover
    audit_emit = None

_TERMINAL_FAILURE_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required", "stale"}


def aggregate_check_runs(check_runs: list[dict]) -> str:
    """green only if ALL check-runs finished with a non-failure conclusion;
    red if ANY of them finished in failure; pending if no check-run exists yet
    or some are still running (decline-never-truncate: we never "guess" green
    before everything has completed)."""
    if not check_runs:
        return "pending"
    saw_failure = False
    for run in check_runs:
        if run.get("status") != "completed":
            return "pending"
        if run.get("conclusion") in _TERMINAL_FAILURE_CONCLUSIONS:
            saw_failure = True
    return "red" if saw_failure else "green"


def consume_ci_status_core(
    github_client: GitHubClient,
    work_item_id: str,
    tenant_id: str,
    repo: str,
    pr_number: int,
    ref: str,
    actor: str = "system:validation",
) -> CiStatusResult:
    check_runs = github_client.list_check_runs(repo, ref)
    status = aggregate_check_runs(check_runs)

    summary = [
        {"name": r.get("name"), "status": r.get("status"), "conclusion": r.get("conclusion")}
        for r in check_runs
    ]
    db.save_ci_status(work_item_id, pr_number, status, {"check_runs": summary})

    if audit_emit is not None:
        audit_emit(
            actor=actor,
            action="ci_status_consumed",
            tenant_id=tenant_id,
            work_item_id=work_item_id,
            details={"repo": repo, "pr_number": pr_number, "status": status, "check_runs": summary},
        )

    return CiStatusResult(work_item_id=work_item_id, pr_number=pr_number, status=status)
