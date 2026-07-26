"""Patch compliance against the plan and immutable SHAs.

The diff is always ``base_sha...head_sha``. Branch names are mutable and may
not even exist in the sandbox clone; accepting them here was the cause of the
regression where ``main...HEAD`` got the WorkItem stuck.
"""
from __future__ import annotations

import re

from dse_contracts import GateStatus, L1Finding, PlanArtifact

from dse_validation.sandbox_exec import SandboxExecutor


class DiffSummary:
    def __init__(
        self,
        files_changed: list[str],
        total_lines_changed: int,
        *,
        base_sha: str,
        head_sha: str,
    ):
        self.files_changed = files_changed
        self.total_lines_changed = total_lines_changed
        self.base_sha = base_sha
        self.head_sha = head_sha


class DiffComputationError(RuntimeError):
    pass


_FULL_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")


def _verify_commit(executor: SandboxExecutor, sha: str, label: str, timeout: int) -> None:
    if not _FULL_GIT_SHA_RE.fullmatch(sha):
        raise DiffComputationError(
            f"{label} must be a full Git SHA of 40 or 64 hexadecimal characters"
        )
    result = executor.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], timeout=timeout)
    if not result.ok:
        raise DiffComputationError(f"{label}={sha} does not exist as a commit in the sandbox")


def compute_diff_summary(
    executor: SandboxExecutor,
    base_sha: str,
    head_sha: str,
    timeout: int = 60,
) -> DiffSummary:
    """``git diff --numstat <base_sha>...<head_sha>`` inside the sandbox — sums
    added+removed lines per file (binary files report "-" in the numstat; we
    count them as a touched file but 0 lines, so diffs with assets don't
    break)."""
    _verify_commit(executor, base_sha, "base_sha", timeout)
    _verify_commit(executor, head_sha, "head_sha", timeout)
    result = executor.run(
        ["git", "diff", "--numstat", f"{base_sha}...{head_sha}"], timeout=timeout
    )
    if result.returncode != 0:
        raise DiffComputationError(
            f"git diff --numstat failed (exit={result.returncode}): {result.stderr.strip()}"
        )
    files: list[str] = []
    total = 0
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, path = parts
        files.append(path)
        if added.isdigit():
            total += int(added)
        if removed.isdigit():
            total += int(removed)
    return DiffSummary(
        files_changed=files,
        total_lines_changed=total,
        base_sha=base_sha,
        head_sha=head_sha,
    )


def _is_forbidden(path: str, forbidden_paths: list[str]) -> str | None:
    for forbidden in forbidden_paths:
        if path == forbidden or path.startswith(forbidden):
            return forbidden
    return None


def diff_budget_finding(diff: DiffSummary, plan: PlanArtifact) -> L1Finding:
    # no_code_change: the plan declares there is NO code change, but the
    # immutable diff changed files — a real inconsistency, so it fails. (This
    # is NOT expected_files; it is about whether a diff exists at all.)
    if plan.no_code_change and diff.files_changed:
        return L1Finding(
            check="diff_budget",
            passed=False,
            status=GateStatus.FAIL,
            detail=(
                "PlanArtifact.no_code_change=true, but the immutable diff "
                f"{diff.base_sha[:12]}...{diff.head_sha[:12]} changed {diff.files_changed}"
            ),
        )

    # OPERATOR DECISION (2026-07-22, 3rd real occurrence): expected_files no
    # longer fails the diff. The Planner predicts files from the TEXT of the
    # issue, BEFORE reading the code; in a bug fix the defect almost always
    # lives in a different layer than the symptom suggests (the issue talked
    # about DELETE /api/transactions → server.js; the bug was in src/store.js —
    # the Coder picked the right file and the gate failed the CORRECT fix).
    #
    # Safety gates that REMAIN (they don't depend on the Planner's prediction):
    #   - line budget (here): real anti-sprawl;
    #   - forbidden_paths: a SEPARATE hard check (migrations/, workflows/…);
    #   - sandbox scoped to the repo; empty plan blocked in the workflow
    #     (patch reject-empty-expected-files-v1), before L1.
    # expected_files is still used to CLASSIFY RISK in the workflow — it just
    # stopped being an equality gate on the diff.
    over_budget = diff.total_lines_changed > plan.diff_budget_lines
    if not over_budget:
        return L1Finding(
            check="diff_budget",
            passed=True,
            detail=(
                f"diff within budget: {diff.total_lines_changed}/{plan.diff_budget_lines} "
                f"lines, {len(diff.files_changed)} file(s) "
                "(expected_files is advisory; forbidden_paths validates the paths)"
            ),
        )
    return L1Finding(
        check="diff_budget",
        passed=False,
        detail=(
            f"diff of {diff.total_lines_changed} lines exceeds the PlanArtifact's "
            f"diff_budget_lines={plan.diff_budget_lines}"
        ),
    )


def forbidden_paths_finding(diff: DiffSummary, plan: PlanArtifact) -> L1Finding:
    violations: list[tuple[str, str]] = []
    for f in diff.files_changed:
        hit = _is_forbidden(f, plan.forbidden_paths)
        if hit:
            violations.append((f, hit))

    if not violations:
        return L1Finding(
            check="forbidden_paths",
            passed=True,
            detail=f"no file touched under the plan's forbidden_paths ({plan.forbidden_paths})",
        )

    detail = "; ".join(
        f"{f} is under a path forbidden by PlanArtifact.forbidden_paths='{hit}'" for f, hit in violations
    )
    return L1Finding(check="forbidden_paths", passed=False, detail=detail)


def plan_compliance_findings(
    executor: SandboxExecutor,
    plan: PlanArtifact,
    base_sha: str,
    head_sha: str,
) -> list[L1Finding]:
    try:
        diff = compute_diff_summary(executor, base_sha, head_sha)
    except DiffComputationError as exc:
        return [
            L1Finding(
                check="git_diff",
                passed=False,
                status=GateStatus.ERROR,
                detail=str(exc),
            )
        ]
    return [diff_budget_finding(diff, plan), forbidden_paths_finding(diff, plan)]
