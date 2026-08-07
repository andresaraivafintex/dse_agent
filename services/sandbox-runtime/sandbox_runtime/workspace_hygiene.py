"""Deterministic post-turn workspace hygiene (P1) — VENDORABLE module.

Extracted from `activities.py` (Phase 1, plan 09) so it can run in TWO places
from a single source of truth: in the worker (Docker runtime, git over the bind
mount) and inside the agent-runner (K8s runtime, git in-pod via
`--op post_turn`). That is why the dependencies are stdlib + `dse_contracts`
only (both present in the runner image) — no docker/temporal/audit here; the
caller is the one that audits, using the returned lists.

The three operations and the real incidents that produced them:
  - `prune_disposable_artifacts`: the CLI creates unsolicited reports
    (BUG_FIX_REPORT.md) — deletes ONLY new disposable junk; legitimate new
    source outside the plan survives (expected_files is advisory since
    2026-07-22).
  - `restore_lockfile_churn`: npm rewrote 16 lines of package-lock.json with no
    change to the paired manifest, and diff_budget failed the task.
  - `revert_test_edits`: the Coder edited a shared test seed and broke a sibling
    test — tests belong to the Tester; any test edit by the Coder is reverted to
    the state at the start of the turn.
"""
from __future__ import annotations

import logging
import os
import subprocess

from dse_contracts.paths import is_disposable_artifact, is_test_path, lockfile_manifest_for

from .scoped_git import NO_CUSTOMER_HOOKS

logger = logging.getLogger("sandbox_runtime.workspace_hygiene")


def prune_disposable_artifacts(
    workspace_dir: str, expected_files: list[str], work_item_id: str
) -> tuple[list[str], list[str]]:
    """Delete NEW (untracked) files that are obvious CLI JUNK before the commit.
    Never touches: what the plan asked for, tests, or the work item's demo.
    Best-effort (L1 is the hard gate). Returns `(pruned, kept_out_of_plan)`."""
    try:
        # `-uall`: list EVERY untracked path individually (without collapsing a
        # new directory into `?? src/`); .gitignore is still respected.
        porcelain = subprocess.run(
            ["git", "status", "--porcelain", "-uall"], cwd=workspace_dir,
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:  # noqa: BLE001 — prune is best-effort; L1 is the hard gate
        logger.warning("post-coder prune failed (git status); L1 remains the gate")
        return [], []

    expected = set(expected_files)
    pruned: list[str] = []
    kept_out_of_plan: list[str] = []
    for line in porcelain.splitlines():
        if not line.startswith("??"):
            continue
        rel = line[3:].strip().strip('"')
        if rel in expected or is_test_path(rel) or rel.startswith(f"demos/{work_item_id}"):
            continue
        if not is_disposable_artifact(rel):
            kept_out_of_plan.append(rel)  # legitimate new source outside the plan — STAYS
            continue
        try:
            os.remove(os.path.join(workspace_dir, rel))
            pruned.append(rel)
        except OSError:
            pass
    return pruned, kept_out_of_plan


def restore_lockfile_churn(workspace_dir: str) -> list[str]:
    """The lockfile changed but the paired manifest (package.json…) did NOT →
    restore (if modified) or remove (if new). Returns the paths handled."""
    try:
        porcelain = subprocess.run(
            ["git", "status", "--porcelain"], cwd=workspace_dir,
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:  # noqa: BLE001 — best-effort; L1 has the same exemption
        return []
    status_by_path: dict[str, str] = {}
    for line in porcelain.splitlines():
        if len(line) > 3:
            status_by_path[line[3:].strip().strip('"')] = line[:2]
    restored: list[str] = []
    for rel, st in sorted(status_by_path.items()):
        manifest = lockfile_manifest_for(rel)
        if manifest is None or manifest in status_by_path:
            continue
        try:
            if st == "??":
                os.remove(os.path.join(workspace_dir, rel))
                restored.append(rel)
            else:
                proc = subprocess.run(
                    ["git", *NO_CUSTOMER_HOOKS, "checkout", "--", rel], cwd=workspace_dir,
                    capture_output=True, text=True, timeout=30,
                )
                if proc.returncode == 0:
                    restored.append(rel)
        except OSError:
            continue
    return restored


def revert_test_edits(workspace_dir: str, turn_start_sha: str) -> list[str]:
    """Revert ANY Coder change under test paths to the state at the start of the
    turn: edit/removal → `git checkout <sha>`; new (untracked) → remove.
    Best-effort. Returns the reverted paths."""
    try:
        porcelain = subprocess.run(
            ["git", "status", "--porcelain", "-uall"], cwd=workspace_dir,
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:  # noqa: BLE001 — best-effort
        logger.warning("revert of the coder's tests failed (git status)")
        return []

    reverted: list[str] = []
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        status, rel = line[:2], line[3:].strip().strip('"')
        if "->" in rel:  # rename: "old -> new" — take the destination
            rel = rel.split("->")[-1].strip()
        if not is_test_path(rel):
            continue
        try:
            if status.strip() == "??":
                os.remove(os.path.join(workspace_dir, rel))
                reverted.append(rel)
            else:
                proc = subprocess.run(
                    ["git", *NO_CUSTOMER_HOOKS, "checkout", turn_start_sha, "--", rel], cwd=workspace_dir,
                    capture_output=True, text=True, timeout=30,
                )
                if proc.returncode == 0:
                    reverted.append(rel)
        except OSError:
            continue
    return reverted
