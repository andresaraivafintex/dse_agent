"""WSE-E6-T16 — merge-base, NEVER rebase during an active human review.

REAL git (local bare repo + clones, like WS-C's git_checkpoint) — nothing mocked.
Real Postgres for the evidence (wse_base_updates) and audit (P8).

The central test is Phase 4's EXIT ASSERTION:
  - builds a PR with base drift + human review threads ANCHORED to commits,
    applies merge-base, and proves `orphaned_threads == 0` (the anchored shas stay
    reachable from the branch tip);
  - and the NEGATIVE test proves that rebase WOULD BREAK it (orphaning the
    threads) — documenting why merge-base is mandatory.
"""
from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from dse_validation import db
from dse_validation.merge_base import (
    count_orphaned_threads,
    is_commit_reachable,
    update_base_branch_core,
)

BASE = "main"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


def _config_identity(repo: Path) -> None:
    _git(repo, "config", "user.email", "coder@dse.local")
    _git(repo, "config", "user.name", "DSE Coder")


def _sha(repo: Path, ref: str = "HEAD") -> str:
    return _git(repo, "rev-parse", ref).stdout.strip()


@pytest.fixture
def ids():
    return f"acme-{uuid.uuid4().hex[:8]}", f"wi_{uuid.uuid4().hex[:8]}"


def _make_scenario(tmp_path: Path, *, conflicting_drift: bool = False):
    """Builds a bare origin + task-branch workspace (2 commits = 2 anchored review
    threads) + drift on the base. Returns (workspace, branch, anchored)."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)

    # seed: base branch with a shared file
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", BASE)
    _config_identity(seed)
    (seed / "shared.py").write_text("base\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "base: shared.py")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", BASE)

    # workspace: clone + task branch with 2 commits (anchored threads)
    workspace = tmp_path / "workspace"
    subprocess.run(["git", "clone", "-q", str(origin), str(workspace)], check=True)
    _config_identity(workspace)
    branch = "dse/task-1"
    _git(workspace, "checkout", "-q", "-b", branch)
    (workspace / "feature_a.py").write_text("def a():\n    return 1\n")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "feat: a")
    anchored_1 = _sha(workspace)
    if conflicting_drift:
        # the branch also touches shared.py (to collide with the base's drift)
        (workspace / "shared.py").write_text("branch-change\n")
    (workspace / "feature_b.py").write_text("def b():\n    return 2\n")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "feat: b")
    anchored_2 = _sha(workspace)
    _git(workspace, "push", "-q", "origin", branch)

    # DRIFT: the base advances while the review is happening
    if conflicting_drift:
        (seed / "shared.py").write_text("main-change\n")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-q", "-m", "base: shared.py conflita")
    else:
        (seed / "drift.py").write_text("# base moved forward\n")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-q", "-m", "base: drift.py")
    _git(seed, "push", "-q", "origin", BASE)

    return workspace, branch, [anchored_1, anchored_2]


# ---------------------------------------------------------------------------
# PHASE 4 EXIT ASSERTION — merge-base preserves the threads (zero orphans).
# ---------------------------------------------------------------------------
def test_merge_base_preserves_review_threads_zero_orphaned(tmp_path, ids):
    tenant_id, work_item_id = ids
    workspace, branch, anchored = _make_scenario(tmp_path)

    # sanity: before the update, the anchored commits are reachable
    for sha in anchored:
        assert is_commit_reachable(str(workspace), sha, branch)

    result = update_base_branch_core(
        work_item_id=work_item_id, tenant_id=tenant_id, repo="acme/repo",
        branch=branch, base_branch=BASE, workspace_dir=str(workspace),
        first_human_review_done=True, anchored_review_shas=anchored,
    )

    assert result.strategy == "merge_base"
    assert result.conflict is False
    # THE ASSERTION: zero orphaned threads.
    assert result.orphaned_threads == 0

    # direct proof against real git: every anchored sha REMAINS reachable from
    # the branch tip (merge preserves history).
    for sha in anchored:
        assert is_commit_reachable(str(workspace), sha, branch), (
            f"thread anchored at {sha[:8]} was orphaned — merge-base should have preserved it"
        )

    # the base was in fact incorporated (drift.py is now on the branch)
    assert (workspace / "drift.py").exists()

    # durable evidence (P8) + audit
    updates = db.list_base_updates(work_item_id)
    assert len(updates) == 1
    assert updates[0]["strategy"] == "merge_base"
    assert updates[0]["orphaned_threads"] == 0
    assert updates[0]["anchored_threads"] == 2
    assert _audit_count(work_item_id, "base_branch_updated") == 1


# ---------------------------------------------------------------------------
# NEGATIVE TEST — rebase WOULD BREAK it (orphaning the threads). Documents why.
# ---------------------------------------------------------------------------
def test_rebase_would_orphan_threads_documented_negative(tmp_path):
    _tenant, _wi = f"t-{uuid.uuid4().hex[:6]}", f"wi_{uuid.uuid4().hex[:6]}"
    workspace, branch, anchored = _make_scenario(tmp_path)

    for sha in anchored:
        assert is_commit_reachable(str(workspace), sha, branch)

    # simulates the FORBIDDEN path: rebasing the branch onto the updated base.
    _git(workspace, "fetch", "-q", "origin", BASE)
    _git(workspace, "rebase", "FETCH_HEAD")

    # after the rebase, the ORIGINAL commits were rewritten (new shas) — the
    # anchored shas are no longer reachable => ALL threads orphaned.
    orphaned = count_orphaned_threads(str(workspace), branch, anchored)
    assert orphaned == anchored, (
        "a rebase should orphan ALL anchored threads (that is why merge-base is "
        "mandatory during human review)"
    )
    assert len(orphaned) == 2


# ---------------------------------------------------------------------------
# Unresolvable conflict => conflict=True, aborts, does NOT force-resolve.
# ---------------------------------------------------------------------------
def test_merge_conflict_escalates_never_force_resolves(tmp_path, ids):
    tenant_id, work_item_id = ids
    workspace, branch, anchored = _make_scenario(tmp_path, conflicting_drift=True)
    tip_before = _sha(workspace, branch)

    result = update_base_branch_core(
        work_item_id=work_item_id, tenant_id=tenant_id, repo="acme/repo",
        branch=branch, base_branch=BASE, workspace_dir=str(workspace),
        first_human_review_done=True, anchored_review_shas=anchored,
    )

    assert result.strategy == "merge_base"
    assert result.conflict is True
    assert result.orphaned_threads == 0  # nothing changed — merge aborted
    # the merge was aborted: no MERGE_HEAD, tip unchanged, clean working tree
    assert not (workspace / ".git" / "MERGE_HEAD").exists()
    assert _sha(workspace, branch) == tip_before
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(workspace), capture_output=True, text=True
    ).stdout.strip()
    assert status == "", "the working tree should be clean after the merge abort"
    assert _audit_count(work_item_id, "base_update_conflict") == 1


# ---------------------------------------------------------------------------
# Rebase is allowed ONLY before the 1st human review (and with no anchored threads).
# ---------------------------------------------------------------------------
def test_rebase_allowed_before_first_human_review(tmp_path, ids):
    tenant_id, work_item_id = ids
    workspace, branch, _anchored = _make_scenario(tmp_path)
    tip_before = _sha(workspace, branch)

    result = update_base_branch_core(
        work_item_id=work_item_id, tenant_id=tenant_id, repo="acme/repo",
        branch=branch, base_branch=BASE, workspace_dir=str(workspace),
        first_human_review_done=False,   # no review has happened yet
        anchored_review_shas=[],         # and there are no anchored threads
    )

    assert result.strategy == "rebase_prefirst_review"
    assert result.conflict is False
    # the branch was rewritten (rebase) — new tip, base incorporated
    assert _sha(workspace, branch) != tip_before
    assert (workspace / "drift.py").exists()


def test_safety_guard_never_rebase_when_threads_exist(tmp_path, ids):
    """Belt-and-suspenders: even with first_human_review_done=False, if anchored
    threads ALREADY exist, the code NEVER rebases — it falls back to merge-base."""
    tenant_id, work_item_id = ids
    workspace, branch, anchored = _make_scenario(tmp_path)

    result = update_base_branch_core(
        work_item_id=work_item_id, tenant_id=tenant_id, repo="acme/repo",
        branch=branch, base_branch=BASE, workspace_dir=str(workspace),
        first_human_review_done=False,   # claims no review happened...
        anchored_review_shas=anchored,   # ...but there are anchored threads
    )
    assert result.strategy == "merge_base"   # it protected the threads
    assert result.orphaned_threads == 0
    for sha in anchored:
        assert is_commit_reachable(str(workspace), sha, branch)


# ---------------------------------------------------------------------------
# No drift => noop.
# ---------------------------------------------------------------------------
def test_noop_when_no_drift(tmp_path, ids):
    tenant_id, work_item_id = ids
    # no-drift scenario: origin/main == ancestor of the branch
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", BASE)
    _config_identity(seed)
    (seed / "x.py").write_text("x\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "base")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", BASE)

    workspace = tmp_path / "workspace"
    subprocess.run(["git", "clone", "-q", str(origin), str(workspace)], check=True)
    _config_identity(workspace)
    branch = "dse/task-noop"
    _git(workspace, "checkout", "-q", "-b", branch)
    (workspace / "f.py").write_text("f\n")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "feat")
    _git(workspace, "push", "-q", "origin", branch)

    result = update_base_branch_core(
        work_item_id=work_item_id, tenant_id=tenant_id, repo="acme/repo",
        branch=branch, base_branch=BASE, workspace_dir=str(workspace),
        first_human_review_done=True, anchored_review_shas=[],
    )
    assert result.strategy == "noop_no_drift"
    assert result.conflict is False
    assert result.orphaned_threads == 0


def _audit_count(work_item_id: str, action: str) -> int:
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM audit_log WHERE work_item_id = %s AND action = %s",
                (work_item_id, action),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()
