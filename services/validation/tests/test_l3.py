"""WSE-E4-T9b — L3 completo: reflexão no tracking comment (<1min, via
MutableCommentWriter da fundação), targeted re-runs em fix commits (API real do
GitHub representada pelo FakeGitHubClient — mesma interface do RealGitHubClient,
que implementa POST .../rerequest de verdade) e episódios de skill-learning de
CI-repair (tenant-scoped, com proveniência; NENHUMA skill criada/ativada).
Postgres real para estado/evidência/audit."""
from __future__ import annotations

import time

from dse_contracts.mutable_comment import MutableCommentWriter

from dse_validation import db
from dse_validation.db import PostgresCommentStateStore
from dse_validation.github.client import FakeGitHubClient
from dse_validation.github.comment_backend import GitHubCommentBackend
from dse_validation.github.l3 import consume_ci_status_l3, failure_signature

REPO = "acme/app"
SURFACE = {"repo": REPO, "issue_number": 42}


def _writer(client: FakeGitHubClient) -> MutableCommentWriter:
    return MutableCommentWriter(
        GitHubCommentBackend(client), PostgresCommentStateStore(), surface="github_pr_ci"
    )


def _runs_red(sha_suffix: str = "") -> list[dict]:
    return [
        {"id": 1, "name": "lint", "status": "completed", "conclusion": "success"},
        {"id": 2, "name": "tests", "status": "completed", "conclusion": "failure",
         "output": {"summary": f"FAILED tests/test_x.py::test_a - AssertionError {sha_suffix}"}},
        {"id": 3, "name": "build", "status": "completed", "conclusion": "failure",
         "output": {"summary": "compile error"}},
    ]


def _runs_green() -> list[dict]:
    return [
        {"id": 11, "name": "lint", "status": "completed", "conclusion": "success"},
        {"id": 12, "name": "tests", "status": "completed", "conclusion": "success"},
        {"id": 13, "name": "build", "status": "completed", "conclusion": "success"},
    ]


def test_reflection_updates_single_tracking_comment_under_1min(work_item_id, tenant_id):
    client = FakeGitHubClient()
    client.set_check_runs(REPO, "sha-a", _runs_red())
    writer = _writer(client)

    started = time.monotonic()
    result = consume_ci_status_l3(
        github_client=client, work_item_id=work_item_id, tenant_id=tenant_id,
        repo=REPO, pr_number=42, ref="sha-a", comment_writer=writer, surface_ref=SURFACE,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 60, "reflexão deve acontecer na mesma chamada (<1min)"
    assert result.status == "red"

    assert len(client._comments) == 1  # EXATAMENTE 1 comentário
    body = next(iter(client._comments.values()))
    assert "`red`" in body and "| tests |" in body and "failure" in body

    # segunda chamada EDITA o mesmo comentário (in-place, nunca comment-per-update)
    client.set_check_runs(REPO, "sha-a", _runs_green())
    consume_ci_status_l3(
        github_client=client, work_item_id=work_item_id, tenant_id=tenant_id,
        repo=REPO, pr_number=42, ref="sha-a", comment_writer=writer, surface_ref=SURFACE,
    )
    assert len(client._comments) == 1
    body2 = next(iter(client._comments.values()))
    assert "`green`" in body2

    # audit da reflexão (P8)
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM audit_log WHERE work_item_id = %s AND action = 'ci_status_reflected'",
                (work_item_id,),
            )
            assert cur.fetchone()[0] == 2
    finally:
        conn.close()


def test_targeted_rerun_only_failed_checks_on_fix_commit(work_item_id, tenant_id):
    client = FakeGitHubClient()
    writer = _writer(client)
    # 1º poll: red em sha-a (checks 2 e 3 falharam)
    client.set_check_runs(REPO, "sha-a", _runs_red())
    consume_ci_status_l3(
        github_client=client, work_item_id=work_item_id, tenant_id=tenant_id,
        repo=REPO, pr_number=42, ref="sha-a", comment_writer=writer, surface_ref=SURFACE,
    )
    # fix commit sha-b chega com os MESMOS checks ainda vermelhos (CI reusou resultado)
    client.set_check_runs(REPO, "sha-b", [
        {"id": 21, "name": "lint", "status": "completed", "conclusion": "success"},
        {"id": 22, "name": "tests", "status": "completed", "conclusion": "failure",
         "output": {"summary": "FAILED tests/test_x.py::test_a"}},
        {"id": 23, "name": "build", "status": "completed", "conclusion": "failure",
         "output": {"summary": "compile error"}},
    ])
    consume_ci_status_l3(
        github_client=client, work_item_id=work_item_id, tenant_id=tenant_id,
        repo=REPO, pr_number=42, ref="sha-b", comment_writer=writer, surface_ref=SURFACE,
    )
    # re-run pedido SÓ para os check-runs falhos do fix commit (22 e 23; nunca o 21)
    assert client.rerequested == [(REPO, 22), (REPO, 23)]
    reruns = db.list_ci_reruns(work_item_id)
    assert len(reruns) == 1
    assert reruns[0]["fix_commit_sha"] == "sha-b"
    assert reruns[0]["check_run_ids"] == [22, 23]
    assert sorted(reruns[0]["check_names"]) == ["build", "tests"]


def test_rerun_gracefully_skipped_when_ci_does_not_support_it(work_item_id, tenant_id):
    client = FakeGitHubClient()
    client.rerequest_supported = False  # CI do repo sem re-run por job (403/422)
    writer = _writer(client)
    client.set_check_runs(REPO, "sha-a", _runs_red())
    consume_ci_status_l3(
        github_client=client, work_item_id=work_item_id, tenant_id=tenant_id,
        repo=REPO, pr_number=42, ref="sha-a", comment_writer=writer, surface_ref=SURFACE,
    )
    client.set_check_runs(REPO, "sha-b", _runs_red())
    result = consume_ci_status_l3(
        github_client=client, work_item_id=work_item_id, tenant_id=tenant_id,
        repo=REPO, pr_number=42, ref="sha-b", comment_writer=writer, surface_ref=SURFACE,
    )
    assert result.status == "red"  # segue sem re-run, nunca explode/bloqueia
    assert client.rerequested == []
    assert db.list_ci_reruns(work_item_id) == []


def test_repair_episode_emitted_on_red_to_green_with_provenance(work_item_id, tenant_id):
    client = FakeGitHubClient()
    writer = _writer(client)
    client.set_check_runs(REPO, "sha-a", _runs_red())
    consume_ci_status_l3(
        github_client=client, work_item_id=work_item_id, tenant_id=tenant_id,
        repo=REPO, pr_number=42, ref="sha-a", comment_writer=writer, surface_ref=SURFACE,
    )
    client.set_check_runs(REPO, "sha-b", _runs_green())
    consume_ci_status_l3(
        github_client=client, work_item_id=work_item_id, tenant_id=tenant_id,
        repo=REPO, pr_number=42, ref="sha-b", comment_writer=writer, surface_ref=SURFACE,
    )
    episodes = db.list_ci_repair_episodes(tenant_id)
    assert len(episodes) == 2  # tests + build (os 2 checks reparados)
    by_check = {e["check_name"]: e for e in episodes}
    assert by_check["tests"]["fix_commit_sha"] == "sha-b"
    prov = by_check["tests"]["provenance"]
    assert prov["repo"] == REPO and prov["failed_ref"] == "sha-a" and prov["fix_ref"] == "sha-b"
    assert all(e["occurrence_n"] == 1 for e in episodes)

    # NENHUMA skill criada/ativada — só o episódio (promoção é Fase 4).
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM skill_registry WHERE tenant_id = %s", (tenant_id,))
            assert cur.fetchone()[0] == 0
    finally:
        conn.close()


def test_repeated_pattern_increments_occurrence_tenant_scoped(work_item_id, tenant_id):
    """O MESMO padrão de falha reparado em outro work item do MESMO tenant
    incrementa occurrence_n; outro tenant começa do 1 (tenant-scoped)."""
    sig = failure_signature("tests", "failure", "FAILED tests/test_x.py::test_a - AssertionError")
    first = db.record_ci_repair_episode(
        tenant_id=tenant_id, work_item_id=work_item_id, check_name="tests",
        failure_signature=sig, fix_commit_sha="sha-1", provenance={"repo": REPO},
    )
    second = db.record_ci_repair_episode(
        tenant_id=tenant_id, work_item_id=f"{work_item_id}-outro", check_name="tests",
        failure_signature=sig, fix_commit_sha="sha-2", provenance={"repo": REPO},
    )
    other_tenant = db.record_ci_repair_episode(
        tenant_id=f"{tenant_id}-b", work_item_id=work_item_id, check_name="tests",
        failure_signature=sig, fix_commit_sha="sha-3", provenance={"repo": REPO},
    )
    assert first["occurrence_n"] == 1
    assert second["occurrence_n"] == 2  # padrão REPETIDO detectado
    assert other_tenant["occurrence_n"] == 1  # nunca cruza tenant


def test_failure_signature_deterministic():
    a = failure_signature("tests", "failure", "FAILED tests/test_x.py::test_a\nmais linhas")
    b = failure_signature("tests", "failure", "failed tests/test_x.py::test_a\noutras linhas")
    c = failure_signature("tests", "failure", "FAILED tests/test_y.py::test_b")
    assert a == b  # 1ª linha normalizada (case) — mesmas falhas agrupam
    assert a != c
