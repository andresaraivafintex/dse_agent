"""WSE-E3-T6 — deterministic PR finalizer: once L1 is green, pushes the branch
under the GitHub App's identity and opens EXACTLY 1 PR per WorkItem from a fixed
template. P1 (deterministic-or-human): no part of this function uses an LLM to
decide anything — title/body come from a fixed template, and the "create or not"
decision is purely based on database/GitHub state.

Idempotency (killing the process midway and re-running must not create a 2nd PR):
  1. check `wse_pr_tracking` (our fast source of truth);
  2. if absent, check the GitHub API for an open PR with head=branch — this covers
     the case where the process dies AFTER `create_pr()` and BEFORE
     `db.save_tracked_pr()` (exactly the window this module's crash-test
     exercises, see tests/test_pr_finalizer_idempotent.py);
  3. only create a new PR if neither source already has one open.
"""
from __future__ import annotations

import re

from dse_contracts import PrRef

from dse_validation import db
from dse_validation.github.client import GitHubClient
from dse_validation.sandbox_exec import SandboxExecutor

try:
    from dse_audit import emit as audit_emit
except ImportError:  # pragma: no cover
    audit_emit = None

STRICT_COMMENT_TEMPLATE = """### Fintex DSE — branch ready for review (strict mode)

The automated gates passed (L1 + L2). In this repository the **PR is opened by a
human** — no agent opens the PR (P1/strict mode, WSE-E3-T8).

- **WorkItem**: `{work_item_id}`
- **Branch**: `{branch}` (already pushed)
- **Open the PR (1 click)**: {compare_url}

When the PR is opened from this link, Fintex DSE correlates and adopts it
automatically (same WorkItem) — the rest of the flow (CI/status, human merge)
stays the same.
"""

PR_TITLE_TEMPLATE = "[DSE {work_item_id}] {summary}"

# Slack encodes mentions and links in its own markup. That markup only renders
# inside Slack: a mention arrives here as `<@U0BJR6U90UF>` and, spelled out in a
# PR title, reads as a raw id nobody can resolve.
_SLACK_MENTION_RE = re.compile(r"<[@#][UWBC][A-Z0-9]+(?:\|[^>]*)?>")
_SLACK_LABELED_LINK_RE = re.compile(r"<https?://[^|>]+\|([^>]+)>")
_SLACK_BARE_LINK_RE = re.compile(r"<(https?://[^>]+)>")


def clean_summary(summary: str) -> str:
    """Strip source-platform markup from text that becomes a PR title or body.

    The summary is the task text exactly as the human typed it on the origin
    platform, so it carries whatever markup that platform uses. Cleaning happens
    at PRESENTATION time only — the faithful snapshot stays in the
    ConversationEvent, which is what the TOCTOU defense (WSA-E2-T2) protects.
    """
    text = _SLACK_MENTION_RE.sub(" ", summary)
    text = _SLACK_LABELED_LINK_RE.sub(r"\1", text)
    text = _SLACK_BARE_LINK_RE.sub(r"\1", text)
    cleaned = " ".join(text.split())
    # A summary that is nothing but markup (a bare "@dse") would clean down to
    # an empty string, and GitHub rejects a PR with an empty title. Keeping the
    # original is ugly but always valid — never trade a cosmetic win for a
    # failed PR creation at the very end of a successful run.
    return cleaned or " ".join(summary.split())


def short_work_item_id(work_item_id: str) -> str:
    """Readable id prefix for the PR title.

    A work item id is `wi_` plus 64 hex chars. Spelled out in the title it eats
    the whole line in GitHub's PR list and buries what the change actually does.
    Nothing correlates by title: the branch name, the PR body and `tracked_prs`
    all carry the full id.
    """
    if work_item_id.startswith("wi_") and len(work_item_id) > 11:
        return work_item_id[:11]
    return work_item_id
PR_BODY_TEMPLATE = """### Fintex DSE — automatically generated PR

- **WorkItem**: `{work_item_id}`
- **Risk class**: `{risk_class}`
- **Summary**: {summary}
- **Test evidence (L1)**: {evidence_url}

{issue_link}

---
This PR was opened by an autonomous agent (Fintex DSE). No agent session
approves or merges its own work (P3) — human review is mandatory before
merge.
"""


def push_branch(executor: SandboxExecutor, github_client: GitHubClient, repo: str, branch: str, timeout: int = 60):
    remote_url = github_client.authenticated_remote_url(repo)
    result = executor.run(["git", "push", "--force-with-lease", remote_url, f"HEAD:refs/heads/{branch}"], timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"git push failed (exit={result.returncode}): {result.stderr.strip()}")
    return result


def compare_url_for(repo: str, base_branch: str, branch: str) -> str:
    """GitHub compare URL that opens the new-PR form already filled in
    (base <- head). `?expand=1` opens the form instead of the diff view (WSE-E3-T8)."""
    return f"https://github.com/{repo}/compare/{base_branch}...{branch}?expand=1"


def _adopt_and_ref(work_item_id: str, existing: dict, *, adopt: bool) -> PrRef:
    if adopt:
        db.adopt_tracked_pr(work_item_id, existing["number"], existing["html_url"])
    return PrRef(work_item_id=work_item_id, pr_number=existing["number"], url=existing["html_url"])


def adopt_pr_core(
    *,
    github_client: GitHubClient,
    work_item_id: str,
    tenant_id: str,
    repo: str,
    branch: str,
    pr_number: int | None = None,
    pr_url: str | None = None,
    actor: str = "system:validation",
) -> PrRef | None:
    """WSE-E3-T8 — called when the workflow detects that a human opened the PR
    from strict mode's compare link. Correlates by branch/WorkItem and ADOPTS the
    PR (fills pr_number on the same tracking row — same WorkItem). Idempotent: it
    only adopts if there was no pr_number yet. Returns the adopted PR's `PrRef`,
    or `None` if no open PR was found yet."""
    if pr_number is None:
        found = github_client.get_open_pr_for_branch(repo, branch)
        if found is None:
            return None
        pr_number, pr_url = found["number"], found["html_url"]
    if pr_url is None:
        pr_url = f"https://github.com/{repo}/pull/{pr_number}"

    tracked = db.get_tracked_pr(work_item_id)
    already = tracked is not None and tracked.get("pr_number") is not None
    if tracked is None:
        db.save_tracked_pr(work_item_id, tenant_id, repo, branch, pr_number, pr_url)
    else:
        db.adopt_tracked_pr(work_item_id, pr_number, pr_url)

    if not already and audit_emit is not None:
        audit_emit(
            actor=actor,
            action="pr_adopted",
            tenant_id=tenant_id,
            work_item_id=work_item_id,
            details={"repo": repo, "branch": branch, "pr_number": pr_number, "url": pr_url},
        )
    return PrRef(work_item_id=work_item_id, pr_number=pr_number, url=pr_url)


def finalize_pr_core(
    *,
    executor: SandboxExecutor,
    github_client: GitHubClient,
    work_item_id: str,
    tenant_id: str,
    repo: str,
    branch: str,
    base_branch: str,
    summary: str,
    risk_class: str,
    evidence_url: str = "",
    issue_ref: dict | None = None,
    actor: str = "system:validation",
    push: bool = True,
    strict_mode: bool = False,
    comment_writer=None,
    surface_ref: dict | None = None,
) -> PrRef:
    # 1) idempotency — our table is the fast source of truth.
    tracked = db.get_tracked_pr(work_item_id)
    if tracked is not None:
        if tracked.get("pr_number") is not None:
            # PR already open (by us in normal mode, or adopted in strict mode).
            # REFINALIZE (review fix cycle, observed live): this path arrives with
            # NEW commits on the branch — without a push, the Coder's fix never
            # showed up on the PR (silent no-op). The push is idempotent
            # (same tip -> up-to-date), so always pushing is safe.
            if push:
                push_branch(executor, github_client, repo, branch)
            return PrRef(work_item_id=work_item_id, pr_number=tracked["pr_number"], url=tracked["pr_url"])
        # Strict mode: compare link already posted, PR not open yet. A human may
        # have opened the PR in the meantime — detect and adopt it.
        existing = github_client.get_open_pr_for_branch(repo, branch)
        if existing is not None:
            return adopt_pr_core(
                github_client=github_client, work_item_id=work_item_id, tenant_id=tenant_id,
                repo=repo, branch=branch, pr_number=existing["number"], pr_url=existing["html_url"],
                actor=actor,
            )
        # Nobody has opened it yet — idempotent: return the SAME compare link.
        cmp_url = tracked.get("compare_url") or compare_url_for(repo, base_branch, branch)
        return PrRef(work_item_id=work_item_id, pr_number=None, url=cmp_url, compare_url=cmp_url)

    # 2) defense in depth: GitHub may already have the PR if a previous run died
    #    between create_pr() and save_tracked_pr() (or a human already opened it).
    existing = github_client.get_open_pr_for_branch(repo, branch)
    if existing is not None:
        db.save_tracked_pr(work_item_id, tenant_id, repo, branch, existing["number"], existing["html_url"])
        return PrRef(work_item_id=work_item_id, pr_number=existing["number"], url=existing["html_url"])

    # 3) push the branch under the GitHub App's identity (both modes push).
    if push:
        push_branch(executor, github_client, repo, branch)

    # 3b) STRICT MODE (WSE-E3-T8): does NOT open the PR — posts a compare link and stops.
    if strict_mode:
        cmp_url = compare_url_for(repo, base_branch, branch)
        db.save_tracked_pr(
            work_item_id, tenant_id, repo, branch, pr_number=None, pr_url=cmp_url, compare_url=cmp_url
        )
        if comment_writer is not None and surface_ref is not None:
            comment_writer.upsert(
                work_item_id,
                surface_ref,
                STRICT_COMMENT_TEMPLATE.format(
                    work_item_id=work_item_id, branch=branch, compare_url=cmp_url
                ),
            )
        if audit_emit is not None:
            audit_emit(
                actor=actor,
                action="pr_compare_link_posted",
                tenant_id=tenant_id,
                work_item_id=work_item_id,
                details={"repo": repo, "branch": branch, "compare_url": cmp_url},
            )
        return PrRef(work_item_id=work_item_id, pr_number=None, url=cmp_url, compare_url=cmp_url)

    # 4) create the PR from the fixed template — back-links the originating issue.
    issue_link = ""
    if issue_ref and issue_ref.get("issue_number"):
        issue_link = f"Closes #{issue_ref['issue_number']}"
    # Title carries the SHORT id (readable in a PR list); the body keeps the full
    # one, which is what a human copies to look the work item up.
    title = PR_TITLE_TEMPLATE.format(
        work_item_id=short_work_item_id(work_item_id), summary=clean_summary(summary)
    )
    body = PR_BODY_TEMPLATE.format(
        work_item_id=work_item_id,
        risk_class=risk_class,
        summary=clean_summary(summary),
        evidence_url=evidence_url or "(no evidence link)",
        issue_link=issue_link or "(no linked source issue)",
    )
    pr = github_client.create_pr(repo, head=branch, base=base_branch, title=title, body=body)

    db.save_tracked_pr(work_item_id, tenant_id, repo, branch, pr["number"], pr["html_url"])

    if audit_emit is not None:
        audit_emit(
            actor=actor,
            action="pr_finalized",
            tenant_id=tenant_id,
            work_item_id=work_item_id,
            details={"repo": repo, "branch": branch, "pr_number": pr["number"], "url": pr["html_url"]},
        )

    return PrRef(work_item_id=work_item_id, pr_number=pr["number"], url=pr["html_url"])
