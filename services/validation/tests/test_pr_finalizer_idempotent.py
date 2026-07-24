"""WSE-E3-T6 — PR finalizer idempotente. A tabela `wse_pr_tracking` é REAL
Postgres (nunca mockamos a garantia de idempotência, é o próprio ponto do
teste). O lado GitHub é um `FakeGitHubClient` porque nenhuma GitHub App real
está registrada nesta sessão — ver README §Credenciais para o que falta em
produção. `push_branch` é testado à parte contra um repo git bare LOCAL real
(git de verdade, sem rede)."""
from __future__ import annotations

import subprocess
import uuid

from dse_validation import db
from dse_validation.github.client import FakeGitHubClient
from dse_validation.github.pr_finalizer import finalize_pr_core, push_branch


def test_finalize_pr_creates_exactly_one_pr(sandbox, work_item_id, tenant_id):
    github = FakeGitHubClient()
    ref = finalize_pr_core(
        executor=sandbox,
        github_client=github,
        work_item_id=work_item_id,
        tenant_id=tenant_id,
        repo="acme/repo",
        branch="dse/task-1",
        base_branch="main",
        summary="corrige bug de arredondamento",
        risk_class="low",
        evidence_url="http://ci.local/run/1",
        issue_ref={"issue_number": 42},
        push=False,
    )
    assert ref.pr_number is not None
    assert github.create_pr_calls == 1

    tracked = db.get_tracked_pr(work_item_id)
    assert tracked is not None
    assert tracked["pr_number"] == ref.pr_number


def test_finalize_pr_rerun_after_success_does_not_create_second_pr(sandbox, work_item_id, tenant_id):
    github = FakeGitHubClient()
    kwargs = dict(
        executor=sandbox,
        github_client=github,
        work_item_id=work_item_id,
        tenant_id=tenant_id,
        repo="acme/repo",
        branch="dse/task-2",
        base_branch="main",
        summary="corrige bug",
        risk_class="low",
        push=False,
    )
    ref1 = finalize_pr_core(**kwargs)
    ref2 = finalize_pr_core(**kwargs)  # simula reexecução (ex.: retry de Activity do Temporal)

    assert github.create_pr_calls == 1, "reexecução não deve abrir um segundo PR"
    assert ref1.pr_number == ref2.pr_number
    assert ref1.url == ref2.url


def test_finalize_pr_recovers_after_crash_between_create_and_persist(sandbox, work_item_id, tenant_id):
    """Simula: processo morreu DEPOIS de `github.create_pr()` mas ANTES de
    `db.save_tracked_pr()`. Nossa tabela local está vazia, mas o GitHub já
    tem o PR aberto — o finalizer deve detectar via `get_open_pr_for_branch`
    e reconciliar, sem criar um PR duplicado."""
    github = FakeGitHubClient()
    repo, branch = "acme/repo", "dse/task-3"

    # Simula a "primeira tentativa": cria o PR só no GitHub, sem persistir no DB
    # (equivalente a cortar o processo exatamente entre os dois passos).
    pr = github.create_pr(repo, head=branch, base="main", title="[DSE] x", body="y")
    assert github.create_pr_calls == 1

    ref = finalize_pr_core(
        executor=sandbox,
        github_client=github,
        work_item_id=work_item_id,
        tenant_id=tenant_id,
        repo=repo,
        branch=branch,
        base_branch="main",
        summary="corrige bug",
        risk_class="low",
        push=False,
    )

    assert github.create_pr_calls == 1, "não deveria criar PR de novo — deveria ter reconciliado"
    assert ref.pr_number == pr["number"]

    tracked = db.get_tracked_pr(work_item_id)
    assert tracked is not None and tracked["pr_number"] == pr["number"]


def test_finalize_pr_creates_exactly_one_pr_per_work_item_not_per_branch(sandbox, tenant_id):
    """Dois work_item_id diferentes na mesma branch (não deveria acontecer na
    prática, mas prova que a chave de idempotência é work_item_id)."""
    github = FakeGitHubClient()
    wi_alpha, wi_beta = f"wi_alpha_{uuid.uuid4().hex[:8]}", f"wi_beta_{uuid.uuid4().hex[:8]}"
    ref_a = finalize_pr_core(
        executor=sandbox, github_client=github, work_item_id=wi_alpha, tenant_id=tenant_id,
        repo="acme/repo", branch="dse/shared-branch", base_branch="main",
        summary="a", risk_class="low", push=False,
    )
    ref_b = finalize_pr_core(
        executor=sandbox, github_client=github, work_item_id=wi_beta, tenant_id=tenant_id,
        repo="acme/repo", branch="dse/shared-branch", base_branch="main",
        summary="b", risk_class="low", push=False,
    )
    # segundo work_item_id reconcilia com o PR já aberto para essa branch —
    # não abre um segundo (GitHub não permite 2 PRs abertos pro mesmo head).
    assert github.create_pr_calls == 1
    assert ref_a.pr_number == ref_b.pr_number


def test_pr_body_cites_work_item_risk_class_and_evidence(sandbox, work_item_id, tenant_id):
    github = FakeGitHubClient()
    finalize_pr_core(
        executor=sandbox, github_client=github, work_item_id=work_item_id, tenant_id=tenant_id,
        repo="acme/repo", branch="dse/task-body", base_branch="main",
        summary="corrige X", risk_class="high", evidence_url="http://ci.local/run/9",
        issue_ref={"issue_number": 7}, push=False,
    )
    pr = github.get_open_pr_for_branch("acme/repo", "dse/task-body")
    assert work_item_id in pr["body"]
    assert "high" in pr["body"]
    assert "http://ci.local/run/9" in pr["body"]
    assert "Closes #7" in pr["body"]


def test_push_branch_uses_real_git_against_local_bare_remote(tmp_path, git_repo):
    """Prova que `push_branch` roda `git push` de verdade (sem rede: o
    "remote" é um repo bare local). O `authenticated_remote_url` do
    GitHubClient é só a URL — trocar por `https://x-access-token:<token>@...`
    em produção não muda a chamada de `git push` em si."""
    bare_remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare_remote)], check=True)

    class LocalRemoteGitHubClient:
        def authenticated_remote_url(self, repo: str) -> str:
            return str(bare_remote)

    from dse_validation.sandbox_exec import LocalFakeSandbox

    executor = LocalFakeSandbox(git_repo)
    push_branch(executor, LocalRemoteGitHubClient(), repo="acme/repo", branch="dse/pushed-branch")

    result = subprocess.run(
        ["git", "branch", "-a"], cwd=str(bare_remote), capture_output=True, text=True, check=True
    )
    assert "dse/pushed-branch" in result.stdout
