"""WSE-E6-T16 — merge-base, NEVER rebase during an active human review.

NEW CONSTRUCTION (finding #2 of addendum 03): Phase 1 described base-drift
handling in the plan but never implemented it; the review loop only re-ran the
Coder on the same branch. This module builds the behavior from scratch.

Problem (failure mode 11, VERIFIED GitHub behavior): when the base branch (main)
advances during an active human review, it is tempting to "update" the task
branch with `git rebase origin/main` + `git push --force`. But rebase REWRITES
the branch's commits (new shas), and GitHub's review threads are ANCHORED to
specific commits — rewriting them ORPHANS the threads (they become "outdated",
losing their anchor). The review conversation is destroyed.

The correct strategy (P1: deterministic, code — never a model):
  - If the base did NOT advance → `noop_no_drift` (nothing to do).
  - If it advanced and the first human review has NOT happened yet
    (`first_human_review_done is False`) AND there is no anchored review thread →
    `rebase_prefirst_review` is safe (there is nothing to orphan): rebase +
    force-with-lease.
  - Otherwise (a review already happened, OR anchored threads already exist) →
    `merge_base`: `git merge origin/main` ON the task branch. Merge PRESERVES the
    original commits (the merge commit has the old tip as a parent) → the anchored
    shas stay reachable → ZERO orphaned threads. Fast-forward push, NO force.
  - Unresolvable merge conflict → `git merge --abort`, `conflict=True`. The
    workflow (WS-B) escalates to a human; the agent NEVER force-resolves (P1/P6).

NOTE: this does NOT break the anti-AUTOMATIC-merge invariant (FR-16). merge-base
updates the TASK BRANCH with the base's drift; merging the PR into the base stays
100% human. They are different things: here the merge is origin/main → task
branch (bringing the drift INTO the task), not task branch → main.

`orphaned_threads` is the Phase 4 exit assertion: it MUST be 0 on the merge-base
path. Measured by comparing the anchored shas BEFORE/AFTER: a thread is orphaned
if the commit it was anchored to stopped being reachable from the branch tip
(merge-base --is-ancestor). merge preserves reachability; rebase breaks it (the
negative test documents this).

Against REAL git (local bare repo + clones, like WS-C's git_checkpoint) — no mock
for the durability/safety guarantees (P8).
"""
from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Sequence

from dse_contracts import UpdateBaseBranchResult

from dse_validation import db

try:
    from dse_audit import emit as audit_emit
except ImportError:  # pragma: no cover
    audit_emit = None

logger = logging.getLogger("dse_validation.merge_base")


class GitError(RuntimeError):
    def __init__(self, argv: list[str], returncode: int, stderr: str):
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"git {' '.join(argv)} failed (exit={returncode}): {stderr.strip()}")


#: Mesma guarda, e mesmo motivo, do `_GIT` em `github/pr_finalizer.py`: hooks do
#: cliente desligados na LINHA DE COMANDO, porque `git config core.hooksPath` é
#: reapontado para `.husky/` assim que o gate L1 roda o `npm ci` do repositório.
#: Aqui pesa em dobro — este módulo faz `merge` e `checkout` no workspace do
#: cliente, depois do gate, que é exatamente a janela em que a config já está
#: desarmada.
NO_CUSTOMER_HOOKS = ("-c", "core.hooksPath=/nonexistent/dse-no-hooks")


def _git(
    workspace_dir: str, *args: str, check: bool = True, timeout: int = 120
) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *NO_CUSTOMER_HOOKS, *args],
        cwd=str(workspace_dir),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise GitError(list(args), proc.returncode, proc.stderr)
    return proc


def _current_sha(workspace_dir: str, ref: str = "HEAD") -> str:
    return _git(workspace_dir, "rev-parse", ref).stdout.strip()


def _ensure_on_branch(workspace_dir: str, branch: str) -> None:
    # checkout the task branch (idempotent — we may already be on it).
    proc = _git(workspace_dir, "rev-parse", "--verify", "--quiet", branch, check=False)
    if proc.returncode == 0:
        _git(workspace_dir, "checkout", "-q", branch)


def is_commit_reachable(workspace_dir: str, sha: str, branch: str) -> bool:
    """True if `sha` is reachable from `branch`'s tip (it is an ancestor or the
    tip itself). This is exactly what decides whether a review thread anchored to
    `sha` stays "alive" (non-orphaned): GitHub keeps the anchor as long as the
    commit is part of the PR's history."""
    proc = _git(workspace_dir, "merge-base", "--is-ancestor", sha, branch, check=False)
    return proc.returncode == 0


def count_orphaned_threads(
    workspace_dir: str, branch: str, anchored_shas: Sequence[str]
) -> list[str]:
    """Returns the anchored shas that became ORPHANED (no longer reachable from
    the branch tip). merge-base preserves all of them → empty list; rebase
    rewrites the commits → every original sha becomes orphaned."""
    orphaned: list[str] = []
    for sha in anchored_shas:
        if not sha:
            continue
        # does the commit still exist in the repo? (rebase does not delete the
        # object immediately, but it stops being reachable — which is what
        # orphans the thread.)
        if not is_commit_reachable(workspace_dir, sha, branch):
            orphaned.append(sha)
    return orphaned


def _fetch_base(workspace_dir: str, base_branch: str) -> str:
    """Fetches the base's current tip from origin and returns the sha (via FETCH_HEAD)."""
    _git(workspace_dir, "fetch", "--quiet", "origin", base_branch)
    return _current_sha(workspace_dir, "FETCH_HEAD")


def _has_drift(workspace_dir: str, branch: str) -> bool:
    """Has the base advanced beyond what the branch already contains? Counts the
    commits in FETCH_HEAD that are NOT reachable from the branch."""
    out = _git(workspace_dir, "rev-list", "--count", f"{branch}..FETCH_HEAD").stdout.strip()
    return int(out or "0") > 0


class MergeBaseConfig:
    """Git repo location for the Activity wrapper (integration seam with WS-C —
    the tests call the core directly with explicit paths, the way LocalFakeSandbox
    does for L1). In production `workspace_dir` is the task workspace of WS-C's
    sandbox and the `origin` remote is the real GitHub repo (GitHub App
    authenticated URL)."""

    def __init__(self) -> None:
        self.git_root = os.environ.get(
            "DSE_WSE_GIT_ROOT",
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "merge_base_repos"),
        )

    def locations(self, work_item_id: str) -> tuple[str, str]:
        # Seam with WS-C (observed live in the review loop): the REAL task
        # workspace lives at $DSE_SANDBOX_STATE_DIR/<wi>/workspace (where the
        # clone/Coder/checkpoint operate). If it exists, that is what merge-base
        # must update — the legacy default (merge_base_repos/, test fixtures)
        # stays as a fallback when the real sandbox is not on this host.
        state_dir = os.environ.get("DSE_SANDBOX_STATE_DIR", "/tmp/dse-sandboxes")
        sandbox_ws = os.path.join(state_dir, work_item_id, "workspace")
        if os.path.isdir(sandbox_ws):
            bare = os.path.join(state_dir, work_item_id, "checkpoint.git")
            return bare, sandbox_ws
        base = os.path.join(self.git_root, work_item_id)
        return os.path.join(base, "origin.git"), os.path.join(base, "workspace")


def update_base_branch_core(
    *,
    work_item_id: str,
    tenant_id: str,
    repo: str,
    branch: str,
    base_branch: str,
    workspace_dir: str,
    first_human_review_done: bool = True,
    anchored_review_shas: Sequence[str] = (),
    actor: str = "system:validation",
    push: bool = True,
    record: bool = True,
) -> UpdateBaseBranchResult:
    """Updates `branch` with `base_branch`'s drift WITHOUT rewriting history when
    there is (or has been) a human review. `workspace_dir` is a clone/checkout of
    the repo with the `origin` remote configured. See the module docstring."""
    anchored = [s for s in anchored_review_shas if s]
    _ensure_on_branch(workspace_dir, branch)
    old_tip = _current_sha(workspace_dir, "HEAD")
    base_tip = _fetch_base(workspace_dir, base_branch)

    # 1) No drift → noop. Nothing to merge, nothing to orphan.
    if not _has_drift(workspace_dir, branch):
        return _finish(
            work_item_id=work_item_id, tenant_id=tenant_id, repo=repo, branch=branch,
            base_branch=base_branch, strategy="noop_no_drift", conflict=False,
            orphaned=[], anchored=anchored, first_human_review_done=first_human_review_done,
            old_tip=old_tip, new_tip=old_tip,
            detail=f"base {base_branch}@{base_tip[:8]} already contained in the branch — nothing to do",
            actor=actor, record=record,
        )

    # 2) Strategy choice — DETERMINISTIC (P1). Rebase is allowed ONLY before the
    #    1st human review AND when there is NO anchored thread (belt-and-
    #    suspenders: even with first_human_review_done=False, if anchored threads
    #    already exist, never rebase — it would orphan them).
    allow_rebase = (not first_human_review_done) and (len(anchored) == 0)

    if allow_rebase:
        proc = _git(workspace_dir, "rebase", "FETCH_HEAD", check=False)
        if proc.returncode != 0:
            _git(workspace_dir, "rebase", "--abort", check=False)
            return _finish(
                work_item_id=work_item_id, tenant_id=tenant_id, repo=repo, branch=branch,
                base_branch=base_branch, strategy="rebase_prefirst_review", conflict=True,
                orphaned=[], anchored=anchored, first_human_review_done=first_human_review_done,
                old_tip=old_tip, new_tip=old_tip,
                detail="conflict during the pre-review rebase — aborted; escalate to a human",
                actor=actor, record=record,
            )
        strategy = "rebase_prefirst_review"
        force = True
    else:
        # git merge origin/main ON the task branch. Preserves history →
        # preserves the threads' anchors → zero orphans.
        proc = _git(workspace_dir, "merge", "--no-edit", "FETCH_HEAD", check=False)
        if proc.returncode != 0:
            _git(workspace_dir, "merge", "--abort", check=False)
            return _finish(
                work_item_id=work_item_id, tenant_id=tenant_id, repo=repo, branch=branch,
                base_branch=base_branch, strategy="merge_base", conflict=True,
                orphaned=[], anchored=anchored, first_human_review_done=first_human_review_done,
                old_tip=old_tip, new_tip=old_tip,
                detail="unresolvable merge conflict — aborted; the workflow escalates to a human "
                       "(the agent NEVER force-resolves)",
                actor=actor, record=record,
            )
        strategy = "merge_base"
        force = False

    new_tip = _current_sha(workspace_dir, "HEAD")
    orphaned = count_orphaned_threads(workspace_dir, branch, anchored)

    if push:
        push_args = ["push", "--quiet"]
        if force:
            push_args.append("--force-with-lease")
        push_args += ["origin", f"{branch}:refs/heads/{branch}"]
        _git(workspace_dir, *push_args)

    return _finish(
        work_item_id=work_item_id, tenant_id=tenant_id, repo=repo, branch=branch,
        base_branch=base_branch, strategy=strategy, conflict=False,
        orphaned=orphaned, anchored=anchored, first_human_review_done=first_human_review_done,
        old_tip=old_tip, new_tip=new_tip,
        detail=f"updated by {strategy}: {old_tip[:8]} -> {new_tip[:8]} "
               f"(base {base_tip[:8]}); orphaned={len(orphaned)}",
        actor=actor, record=record,
    )


def _finish(
    *, work_item_id, tenant_id, repo, branch, base_branch, strategy, conflict,
    orphaned, anchored, first_human_review_done, old_tip, new_tip, detail, actor, record,
) -> UpdateBaseBranchResult:
    if record:
        db.record_base_update(
            work_item_id=work_item_id, tenant_id=tenant_id, repo=repo, branch=branch,
            base_branch=base_branch, strategy=strategy, conflict=conflict,
            orphaned_threads=len(orphaned), anchored_threads=len(anchored),
            first_human_review_done=first_human_review_done,
            old_tip_sha=old_tip, new_tip_sha=new_tip, detail=detail,
        )
    if audit_emit is not None:
        action = "base_update_conflict" if conflict else "base_branch_updated"
        audit_emit(
            actor=actor, action=action, tenant_id=tenant_id, work_item_id=work_item_id,
            details={
                "repo": repo, "branch": branch, "base_branch": base_branch,
                "strategy": strategy, "conflict": conflict,
                "orphaned_threads": len(orphaned), "orphaned_shas": orphaned,
                "anchored_threads": len(anchored),
                "first_human_review_done": first_human_review_done,
                "old_tip": old_tip, "new_tip": new_tip, "detail": detail,
            },
        )
    return UpdateBaseBranchResult(
        work_item_id=work_item_id, strategy=strategy, conflict=conflict,
        orphaned_threads=len(orphaned), detail=detail,
    )
