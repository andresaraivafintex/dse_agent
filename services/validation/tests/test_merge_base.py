"""WSE-E6-T16 — merge-base, NUNCA rebase durante review humano ativo.

Git REAL (bare repo local + clones, como o git_checkpoint do WS-C) — nada
mockado. Postgres real para a evidência (wse_base_updates) e audit (P8).

O teste central é a ASSERÇÃO DE EXIT da Fase 4:
  - cria um PR com drift de base + threads de review humanas ANCORADAS em
    commits, aplica merge-base, e prova `orphaned_threads == 0` (os shas
    ancorados continuam alcançáveis a partir do tip do branch);
  - e o teste NEGATIVO prova que rebase QUEBRARIA (orfanaria as threads) —
    documentando por que merge-base é obrigatório.
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
    """Monta origin bare + workspace do branch da tarefa (2 commits = 2 threads
    de review ancoradas) + drift na base. Retorna (workspace, branch, anchored)."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)

    # seed: base branch com um arquivo compartilhado
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", BASE)
    _config_identity(seed)
    (seed / "shared.py").write_text("base\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "base: shared.py")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", BASE)

    # workspace: clone + branch da tarefa com 2 commits (threads ancoradas)
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
        # o branch também toca shared.py (para colidir com o drift da base)
        (workspace / "shared.py").write_text("branch-change\n")
    (workspace / "feature_b.py").write_text("def b():\n    return 2\n")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "feat: b")
    anchored_2 = _sha(workspace)
    _git(workspace, "push", "-q", "origin", branch)

    # DRIFT: a base avança enquanto o review acontece
    if conflicting_drift:
        (seed / "shared.py").write_text("main-change\n")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-q", "-m", "base: shared.py conflita")
    else:
        (seed / "drift.py").write_text("# base avançou\n")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-q", "-m", "base: drift.py")
    _git(seed, "push", "-q", "origin", BASE)

    return workspace, branch, [anchored_1, anchored_2]


# ---------------------------------------------------------------------------
# ASSERÇÃO DE EXIT DA FASE 4 — merge-base preserva as threads (zero órfãs).
# ---------------------------------------------------------------------------
def test_merge_base_preserves_review_threads_zero_orphaned(tmp_path, ids):
    tenant_id, work_item_id = ids
    workspace, branch, anchored = _make_scenario(tmp_path)

    # sanidade: antes da atualização, os commits ancorados são alcançáveis
    for sha in anchored:
        assert is_commit_reachable(str(workspace), sha, branch)

    result = update_base_branch_core(
        work_item_id=work_item_id, tenant_id=tenant_id, repo="acme/repo",
        branch=branch, base_branch=BASE, workspace_dir=str(workspace),
        first_human_review_done=True, anchored_review_shas=anchored,
    )

    assert result.strategy == "merge_base"
    assert result.conflict is False
    # A ASSERÇÃO: zero threads órfãs.
    assert result.orphaned_threads == 0

    # prova direta contra o git real: cada sha ancorado CONTINUA alcançável
    # a partir do tip do branch (merge preserva a história).
    for sha in anchored:
        assert is_commit_reachable(str(workspace), sha, branch), (
            f"thread ancorada em {sha[:8]} ficou órfã — merge-base deveria preservá-la"
        )

    # a base foi de fato incorporada (drift.py agora está no branch)
    assert (workspace / "drift.py").exists()

    # evidência durável (P8) + audit
    updates = db.list_base_updates(work_item_id)
    assert len(updates) == 1
    assert updates[0]["strategy"] == "merge_base"
    assert updates[0]["orphaned_threads"] == 0
    assert updates[0]["anchored_threads"] == 2
    assert _audit_count(work_item_id, "base_branch_updated") == 1


# ---------------------------------------------------------------------------
# TESTE NEGATIVO — rebase QUEBRARIA (orfanaria as threads). Documenta o porquê.
# ---------------------------------------------------------------------------
def test_rebase_would_orphan_threads_documented_negative(tmp_path):
    _tenant, _wi = f"t-{uuid.uuid4().hex[:6]}", f"wi_{uuid.uuid4().hex[:6]}"
    workspace, branch, anchored = _make_scenario(tmp_path)

    for sha in anchored:
        assert is_commit_reachable(str(workspace), sha, branch)

    # simula o caminho PROIBIDO: rebase do branch sobre a base atualizada.
    _git(workspace, "fetch", "-q", "origin", BASE)
    _git(workspace, "rebase", "FETCH_HEAD")

    # depois do rebase, os commits ORIGINAIS foram reescritos (novos shas) —
    # os shas ancorados NÃO são mais alcançáveis => TODAS as threads órfãs.
    orphaned = count_orphaned_threads(str(workspace), branch, anchored)
    assert orphaned == anchored, (
        "rebase deveria orfanar TODAS as threads ancoradas (é por isso que "
        "merge-base é obrigatório durante review humano)"
    )
    assert len(orphaned) == 2


# ---------------------------------------------------------------------------
# Conflito não-resolvível => conflict=True, aborta, NÃO resolve à força.
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
    assert result.orphaned_threads == 0  # nada mudou — merge abortado
    # o merge foi abortado: sem MERGE_HEAD, tip inalterado, working tree limpa
    assert not (workspace / ".git" / "MERGE_HEAD").exists()
    assert _sha(workspace, branch) == tip_before
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(workspace), capture_output=True, text=True
    ).stdout.strip()
    assert status == "", "working tree deveria estar limpa após o abort do merge"
    assert _audit_count(work_item_id, "base_update_conflict") == 1


# ---------------------------------------------------------------------------
# Rebase é permitido SÓ antes do 1º review humano (e sem threads ancoradas).
# ---------------------------------------------------------------------------
def test_rebase_allowed_before_first_human_review(tmp_path, ids):
    tenant_id, work_item_id = ids
    workspace, branch, _anchored = _make_scenario(tmp_path)
    tip_before = _sha(workspace, branch)

    result = update_base_branch_core(
        work_item_id=work_item_id, tenant_id=tenant_id, repo="acme/repo",
        branch=branch, base_branch=BASE, workspace_dir=str(workspace),
        first_human_review_done=False,   # ainda não houve review
        anchored_review_shas=[],         # e não há threads ancoradas
    )

    assert result.strategy == "rebase_prefirst_review"
    assert result.conflict is False
    # o branch foi reescrito (rebase) — tip novo, base incorporada
    assert _sha(workspace, branch) != tip_before
    assert (workspace / "drift.py").exists()


def test_safety_guard_never_rebase_when_threads_exist(tmp_path, ids):
    """Belt-and-suspenders: mesmo com first_human_review_done=False, se JÁ
    existem threads ancoradas, o código NUNCA rebase — cai em merge-base."""
    tenant_id, work_item_id = ids
    workspace, branch, anchored = _make_scenario(tmp_path)

    result = update_base_branch_core(
        work_item_id=work_item_id, tenant_id=tenant_id, repo="acme/repo",
        branch=branch, base_branch=BASE, workspace_dir=str(workspace),
        first_human_review_done=False,   # diz que não houve review...
        anchored_review_shas=anchored,   # ...mas há threads ancoradas
    )
    assert result.strategy == "merge_base"   # protegeu as threads
    assert result.orphaned_threads == 0
    for sha in anchored:
        assert is_commit_reachable(str(workspace), sha, branch)


# ---------------------------------------------------------------------------
# Sem drift => noop.
# ---------------------------------------------------------------------------
def test_noop_when_no_drift(tmp_path, ids):
    tenant_id, work_item_id = ids
    # cenário sem drift: origin/main == ancestral do branch
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
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
