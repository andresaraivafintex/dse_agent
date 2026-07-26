"""WSE-E3-T7 — GitHub backend for `MutableCommentWriter` (already available in
the foundation, reused here — we do not reimplement the mutable comment). Ref
state is REAL Postgres (`wse_comment_refs`); transport is `FakeGitHubClient`
(no real GitHub App in this session)."""
from __future__ import annotations

from dse_contracts import MutableCommentWriter

from dse_validation.github.client import FakeGitHubClient
from dse_validation.github.comment_backend import GitHubCommentBackend
from dse_validation.db import PostgresCommentStateStore


def test_first_upsert_creates_comment(work_item_id):
    github = FakeGitHubClient()
    writer = MutableCommentWriter(
        backend=GitHubCommentBackend(github), store=PostgresCommentStateStore(), surface="github_pr_tracking"
    )
    surface_ref = {"repo": "acme/repo", "issue_number": 1}

    ref = writer.upsert(work_item_id, surface_ref, "status: implementing")
    assert github._comments[ref] == "status: implementing"


def test_second_upsert_edits_in_place_not_a_new_comment(work_item_id):
    github = FakeGitHubClient()
    writer = MutableCommentWriter(
        backend=GitHubCommentBackend(github), store=PostgresCommentStateStore(), surface="github_pr_tracking"
    )
    surface_ref = {"repo": "acme/repo", "issue_number": 2}

    ref1 = writer.upsert(work_item_id, surface_ref, "status: implementing")
    ref2 = writer.upsert(work_item_id, surface_ref, "status: L1 green")

    assert ref1 == ref2, "must edit the same comment, never create a second one"
    assert len(github._comments) == 1
    assert github._comments[ref1] == "status: L1 green"


def test_upsert_persists_ref_across_writer_instances(work_item_id):
    """Proves the ref survives a "process restart" (a new writer instance using
    the SAME real PostgresCommentStateStore) — this is the crash-consistency
    guarantee that MutableCommentWriter documents."""
    github = FakeGitHubClient()
    surface_ref = {"repo": "acme/repo", "issue_number": 3}

    writer1 = MutableCommentWriter(
        backend=GitHubCommentBackend(github), store=PostgresCommentStateStore(), surface="github_pr_tracking"
    )
    ref1 = writer1.upsert(work_item_id, surface_ref, "first message")

    writer2 = MutableCommentWriter(
        backend=GitHubCommentBackend(github), store=PostgresCommentStateStore(), surface="github_pr_tracking"
    )
    ref2 = writer2.upsert(work_item_id, surface_ref, "second message (post-restart)")

    assert ref1 == ref2
    assert len(github._comments) == 1
    assert github._comments[ref1] == "second message (post-restart)"
