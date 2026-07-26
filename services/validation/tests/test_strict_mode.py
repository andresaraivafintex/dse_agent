"""WSE-E3-T8 — strict mode: the human opens the PR (unblocked by PrRef.compare_url).

REAL Postgres (idempotency/tracking is never mocked, P8). GitHub is
`FakeGitHubClient` (no real App, see README). `push=False` because the point of
these tests is the compare-link/adoption behavior, not `git push` (that one is
tested separately against a real local bare remote in
test_pr_finalizer_idempotent.py)."""
from __future__ import annotations


from dse_contracts.mutable_comment import MutableCommentWriter

from dse_validation import db
from dse_validation.config import StrictModeConfig
from dse_validation.db import PostgresCommentStateStore
from dse_validation.github.client import FakeGitHubClient
from dse_validation.github.comment_backend import GitHubCommentBackend
from dse_validation.github.pr_finalizer import adopt_pr_core, compare_url_for, finalize_pr_core


def _finalize(github, sandbox, work_item_id, tenant_id, *, strict, writer=None, surface=None, branch="dse/strict-1"):
    return finalize_pr_core(
        executor=sandbox,
        github_client=github,
        work_item_id=work_item_id,
        tenant_id=tenant_id,
        repo="acme/repo",
        branch=branch,
        base_branch="main",
        summary="corrige X",
        risk_class="low",
        push=False,
        strict_mode=strict,
        comment_writer=writer,
        surface_ref=surface,
    )


def test_strict_mode_returns_compare_url_and_no_pr(sandbox, work_item_id, tenant_id):
    github = FakeGitHubClient()
    ref = _finalize(github, sandbox, work_item_id, tenant_id, strict=True)

    assert ref.pr_number is None
    assert ref.compare_url is not None
    assert ref.compare_url == compare_url_for("acme/repo", "main", "dse/strict-1")
    assert ref.url == ref.compare_url
    # NEVER opens the PR in strict mode.
    assert github.create_pr_calls == 0

    tracked = db.get_tracked_pr(work_item_id)
    assert tracked is not None
    assert tracked["pr_number"] is None
    assert tracked["compare_url"] == ref.compare_url


def test_strict_mode_posts_compare_link_in_tracking_comment(sandbox, work_item_id, tenant_id):
    github = FakeGitHubClient()
    writer = MutableCommentWriter(
        GitHubCommentBackend(github), PostgresCommentStateStore(), surface="github_pr"
    )
    surface = {"repo": "acme/repo", "issue_number": 42}
    ref = _finalize(github, sandbox, work_item_id, tenant_id, strict=True, writer=writer, surface=surface)

    # the single tracking comment contains the compare link.
    assert len(github._comments) == 1
    body = next(iter(github._comments.values()))
    assert ref.compare_url in body
    assert "strict mode" in body


def test_strict_mode_idempotent_rerun_same_compare_no_pr(sandbox, work_item_id, tenant_id):
    github = FakeGitHubClient()
    ref1 = _finalize(github, sandbox, work_item_id, tenant_id, strict=True)
    ref2 = _finalize(github, sandbox, work_item_id, tenant_id, strict=True)
    assert ref1.compare_url == ref2.compare_url
    assert ref2.pr_number is None
    assert github.create_pr_calls == 0


def test_human_opens_pr_then_finalizer_adopts_same_work_item(sandbox, work_item_id, tenant_id):
    """Full flow: strict mode posts the compare link -> the human opens the PR ->
    a finalizer re-run detects the open PR and ADOPTS it (same WorkItem, same
    tracking row, pr_number filled in)."""
    github = FakeGitHubClient()
    ref_strict = _finalize(github, sandbox, work_item_id, tenant_id, strict=True)
    assert ref_strict.pr_number is None

    # the human opens the PR from the compare link (outside our control).
    human_pr = github.create_pr("acme/repo", head="dse/strict-1", base="main", title="[DSE] x", body="y")
    assert github.create_pr_calls == 1

    # the finalizer re-run (strict mode) detects and adopts it.
    ref_adopted = _finalize(github, sandbox, work_item_id, tenant_id, strict=True)
    assert ref_adopted.pr_number == human_pr["number"]

    tracked = db.get_tracked_pr(work_item_id)
    assert tracked["pr_number"] == human_pr["number"]
    # did not open a second PR.
    assert github.create_pr_calls == 1


def test_adopt_pr_core_returns_none_when_no_pr_yet(sandbox, work_item_id, tenant_id):
    github = FakeGitHubClient()
    _finalize(github, sandbox, work_item_id, tenant_id, strict=True)
    result = adopt_pr_core(
        github_client=github, work_item_id=work_item_id, tenant_id=tenant_id,
        repo="acme/repo", branch="dse/strict-1",
    )
    assert result is None  # nobody has opened the PR yet


def test_adopt_pr_core_idempotent(sandbox, work_item_id, tenant_id):
    github = FakeGitHubClient()
    _finalize(github, sandbox, work_item_id, tenant_id, strict=True)
    github.create_pr("acme/repo", head="dse/strict-1", base="main", title="t", body="b")

    r1 = adopt_pr_core(
        github_client=github, work_item_id=work_item_id, tenant_id=tenant_id,
        repo="acme/repo", branch="dse/strict-1",
    )
    r2 = adopt_pr_core(
        github_client=github, work_item_id=work_item_id, tenant_id=tenant_id,
        repo="acme/repo", branch="dse/strict-1",
    )
    assert r1.pr_number == r2.pr_number
    tracked = db.get_tracked_pr(work_item_id)
    assert tracked["pr_number"] == r1.pr_number


def test_non_strict_mode_still_opens_pr(sandbox, work_item_id, tenant_id):
    """Regression: with strict=False the Phase 1 behavior is unchanged."""
    github = FakeGitHubClient()
    ref = _finalize(github, sandbox, work_item_id, tenant_id, strict=False)
    assert ref.pr_number is not None
    assert github.create_pr_calls == 1


# --- flag resolution per repo/tenant ---------------------------------------
def test_strict_config_global_default_off(monkeypatch):
    monkeypatch.delenv("DSE_WSE_STRICT_MODE", raising=False)
    monkeypatch.delenv("DSE_WSE_STRICT_MODE_REPOS", raising=False)
    cfg = StrictModeConfig()
    assert cfg.is_strict_for("acme", "acme/repo") is False


def test_strict_config_global_on(monkeypatch):
    monkeypatch.setenv("DSE_WSE_STRICT_MODE", "true")
    cfg = StrictModeConfig()
    assert cfg.is_strict_for("acme", "acme/repo") is True


def test_strict_config_per_tenant_repo_wins(monkeypatch):
    monkeypatch.setenv("DSE_WSE_STRICT_MODE", "false")
    monkeypatch.setenv("DSE_WSE_STRICT_MODE_TENANT_ACME_ACME_REPO", "true")
    cfg = StrictModeConfig()
    assert cfg.is_strict_for("acme", "acme/repo") is True
    # another repo of the same tenant does not inherit it.
    assert cfg.is_strict_for("acme", "acme/other") is False


def test_strict_config_repo_allowlist(monkeypatch):
    monkeypatch.setenv("DSE_WSE_STRICT_MODE", "false")
    monkeypatch.delenv("DSE_WSE_STRICT_MODE_TENANT_ACME", raising=False)
    monkeypatch.setenv("DSE_WSE_STRICT_MODE_REPOS", "acme:acme/repo, beta:beta/svc")
    cfg = StrictModeConfig()
    assert cfg.is_strict_for("acme", "acme/repo") is True
    assert cfg.is_strict_for("beta", "beta/svc") is True
    assert cfg.is_strict_for("acme", "acme/other") is False
