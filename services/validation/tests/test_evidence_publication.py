"""WSE-E5-T14 — consolidated publication + debounce (ADR-26). Real Postgres +
Garage; GitHub stood in for by FakeGitHubClient (same interface as the real one)."""
from __future__ import annotations

from pathlib import Path

from dse_contracts.activities import PublishArtifactInput
from dse_contracts.mutable_comment import MutableCommentWriter

from dse_validation import db
from dse_validation.db import PostgresCommentStateStore
from dse_validation.evidence.garage import publish_artifact_core
from dse_validation.evidence.publication import publish_evidence_bundle, should_refresh_evidence
from dse_validation.github.client import FakeGitHubClient
from dse_validation.github.comment_backend import GitHubCommentBackend

REPO = "acme/app"
SURFACE = {"repo": REPO, "issue_number": 42}


def _writer(client: FakeGitHubClient) -> MutableCommentWriter:
    return MutableCommentWriter(
        GitHubCommentBackend(client), PostgresCommentStateStore(), surface="github_pr_evidence"
    )


def _seed_artifact(work_item_id: str, tenant_id: str, kind: str, tmp_path: Path) -> str:
    f = tmp_path / f"{kind}.bin"
    f.write_bytes(b"evidence-" + kind.encode())
    return publish_artifact_core(
        PublishArtifactInput(
            work_item_id=work_item_id, tenant_id=tenant_id, kind=kind,
            local_path=str(f), ttl_seconds=600,
        )
    ).store_key


# ---------------------------------------------------------------------------
# Debounce decision (deterministic, P1) — contract consumed by WS-B
# ---------------------------------------------------------------------------
def test_first_publication_always_refreshes(work_item_id):
    d = should_refresh_evidence(work_item_id=work_item_id, commit_sha="sha-1")
    assert d.refresh and "first evidence publication" in d.reason


def test_same_commit_is_debounced_and_human_request_overrides(work_item_id, tenant_id, tmp_path):
    client = FakeGitHubClient()
    _seed_artifact(work_item_id, tenant_id, "test_report", tmp_path)
    out = publish_evidence_bundle(
        work_item_id=work_item_id, tenant_id=tenant_id, commit_sha="sha-1",
        comment_writer=_writer(client), surface_ref=SURFACE, pr_number=42,
    )
    assert out["published"] is True

    # same commit => debounce (no refresh, no edit)
    d = should_refresh_evidence(work_item_id=work_item_id, commit_sha="sha-1")
    assert d.refresh is False and "debounce" in d.reason
    out2 = publish_evidence_bundle(
        work_item_id=work_item_id, tenant_id=tenant_id, commit_sha="sha-1",
        comment_writer=_writer(client), surface_ref=SURFACE, pr_number=42,
    )
    assert out2["published"] is False

    # an EXPLICIT human request pierces the debounce (ADR-26)
    d_human = should_refresh_evidence(
        work_item_id=work_item_id, commit_sha="sha-1", human_requested=True
    )
    assert d_human.refresh is True and "explicit human request" in d_human.reason


def test_docs_only_commit_is_debounced_behavior_change_is_not(work_item_id, tenant_id, tmp_path):
    client = FakeGitHubClient()
    _seed_artifact(work_item_id, tenant_id, "test_report", tmp_path)
    publish_evidence_bundle(
        work_item_id=work_item_id, tenant_id=tenant_id, commit_sha="sha-1",
        comment_writer=_writer(client), surface_ref=SURFACE,
    )
    # new commit touching ONLY docs => does not regenerate evidence (ADR-26)
    d_docs = should_refresh_evidence(
        work_item_id=work_item_id, commit_sha="sha-2",
        files_changed=["README.md", "docs/guide.md"],
    )
    assert d_docs.refresh is False
    # new commit that changes behavior => refresh
    d_code = should_refresh_evidence(
        work_item_id=work_item_id, commit_sha="sha-2",
        files_changed=["README.md", "api/handler.py"],
    )
    assert d_code.refresh is True


# ---------------------------------------------------------------------------
# Consolidated publication: 1 tracking comment with ALL the links
# ---------------------------------------------------------------------------
def test_consolidated_comment_has_video_trace_diff_preview_and_logs_access(
    work_item_id, tenant_id, tmp_path
):
    client = FakeGitHubClient()
    video_key = _seed_artifact(work_item_id, tenant_id, "demo_video", tmp_path)
    trace_key = _seed_artifact(work_item_id, tenant_id, "playwright_trace", tmp_path)
    diff_key = _seed_artifact(work_item_id, tenant_id, "visual_diff", tmp_path)
    db.upsert_preview(
        work_item_id=work_item_id, tenant_id=tenant_id, pr_number=42, repo=REPO,
        status="created", namespace=f"preview-{work_item_id}",
        url=f"http://preview.preview-{work_item_id}.svc.cluster.local",
    )
    db.save_ci_status(work_item_id, 42, "green", {"check_runs": []})

    out = publish_evidence_bundle(
        work_item_id=work_item_id, tenant_id=tenant_id, commit_sha="sha-1",
        comment_writer=_writer(client), surface_ref=SURFACE, pr_number=42,
    )
    assert out["published"] is True

    assert len(client._comments) == 1  # ONE consolidated comment
    body = next(iter(client._comments.values()))
    for key in (video_key, trace_key, diff_key):
        assert key in body, f"link for {key} missing from the consolidated comment"
    assert f"preview-{work_item_id}" in body  # preview link
    assert "green" in body  # CI status reflected

    # every rendered link produced an ACCESS LOG attributable to the PR (via=tracking_comment)
    accesses = [a for a in db.list_artifact_accesses(work_item_id) if a["via"] == "tracking_comment"]
    assert len(accesses) == 3
    assert all(a["pr_number"] == 42 for a in accesses)

    # second publication (new commit) EDITS the same comment
    publish_evidence_bundle(
        work_item_id=work_item_id, tenant_id=tenant_id, commit_sha="sha-2",
        comment_writer=_writer(client), surface_ref=SURFACE, pr_number=42,
        files_changed=["api/handler.py"],
    )
    assert len(client._comments) == 1


def test_quarantined_artifact_link_is_revoked_in_comment(work_item_id, tenant_id, tmp_path):
    """A quarantined artifact never becomes a link in the comment — it shows up
    as revoked (integrates T12 <-> T14)."""
    from dse_validation.evidence.garage import quarantine_artifacts_for_work_item

    client = FakeGitHubClient()
    _seed_artifact(work_item_id, tenant_id, "demo_video", tmp_path)
    quarantine_artifacts_for_work_item(work_item_id)

    out = publish_evidence_bundle(
        work_item_id=work_item_id, tenant_id=tenant_id, commit_sha="sha-1",
        comment_writer=_writer(client), surface_ref=SURFACE, pr_number=42,
    )
    assert out["published"] is True
    body = next(iter(client._comments.values()))
    assert "quarantined" in body
    assert "http://localhost:3900" not in body  # no presigned link leaked
