"""WSE-E3-T7 — backend GitHub para `MutableCommentWriter` (já pronto na
fundação, reutilizado aqui — não reimplementamos comentário mutável). Estado
de ref é REAL Postgres (`wse_comment_refs`); transporte é `FakeGitHubClient`
(sem GitHub App real nesta sessão)."""
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

    ref = writer.upsert(work_item_id, surface_ref, "status: implementando")
    assert github._comments[ref] == "status: implementando"


def test_second_upsert_edits_in_place_not_a_new_comment(work_item_id):
    github = FakeGitHubClient()
    writer = MutableCommentWriter(
        backend=GitHubCommentBackend(github), store=PostgresCommentStateStore(), surface="github_pr_tracking"
    )
    surface_ref = {"repo": "acme/repo", "issue_number": 2}

    ref1 = writer.upsert(work_item_id, surface_ref, "status: implementando")
    ref2 = writer.upsert(work_item_id, surface_ref, "status: L1 verde")

    assert ref1 == ref2, "deve editar o mesmo comentário, nunca criar um segundo"
    assert len(github._comments) == 1
    assert github._comments[ref1] == "status: L1 verde"


def test_upsert_persists_ref_across_writer_instances(work_item_id):
    """Prova que o ref sobrevive a um "restart do processo" (nova instância do
    writer usando o MESMO PostgresCommentStateStore real) — é a garantia de
    crash-consistency que o MutableCommentWriter documenta."""
    github = FakeGitHubClient()
    surface_ref = {"repo": "acme/repo", "issue_number": 3}

    writer1 = MutableCommentWriter(
        backend=GitHubCommentBackend(github), store=PostgresCommentStateStore(), surface="github_pr_tracking"
    )
    ref1 = writer1.upsert(work_item_id, surface_ref, "primeira mensagem")

    writer2 = MutableCommentWriter(
        backend=GitHubCommentBackend(github), store=PostgresCommentStateStore(), surface="github_pr_tracking"
    )
    ref2 = writer2.upsert(work_item_id, surface_ref, "segunda mensagem (pós-restart)")

    assert ref1 == ref2
    assert len(github._comments) == 1
    assert github._comments[ref1] == "segunda mensagem (pós-restart)"
