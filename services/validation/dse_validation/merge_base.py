"""WSE-E6-T16 — merge-base, NUNCA rebase durante um review humano ativo.

CONSTRUÇÃO NOVA (achado #2 do adendo 03): a Fase 1 descreveu o tratamento de
base-drift no plano mas nunca o implementou; o review loop só re-rodava o Coder
no mesmo branch. Este módulo constrói o comportamento do zero.

Problema (failure mode 11, comportamento VERIFICADO do GitHub): quando a base
branch (main) avança durante um review humano ativo, é tentador "atualizar" o
branch da tarefa com `git rebase origin/main` + `git push --force`. Mas rebase
REESCREVE os commits do branch (novos shas), e as threads de review do GitHub
estão ANCORADAS em commits específicos — reescrevê-los ÓRFÃ as threads (viram
"outdated", perdem a âncora). A conversa de review é destruída.

A estratégia correta (P1: determinística, código — nunca modelo):
  - Se a base NÃO avançou → `noop_no_drift` (nada a fazer).
  - Se avançou e AINDA NÃO houve o primeiro review humano (`first_human_review_done
    is False`) E não há nenhuma thread de review ancorada → `rebase_prefirst_review`
    é seguro (não há o que orfanar): rebase + force-with-lease.
  - Caso contrário (já houve review, OU já existem threads ancoradas) →
    `merge_base`: `git merge origin/main` NO branch da tarefa. Merge PRESERVA os
    commits originais (o merge commit tem o tip antigo como pai) → os shas
    ancorados continuam alcançáveis → ZERO threads órfãs. Push fast-forward, SEM
    force.
  - Conflito de merge não-resolvível → `git merge --abort`, `conflict=True`. O
    workflow (WS-B) escala a um humano; o agente NUNCA resolve à força (P1/P6).

NOTA: isto NÃO quebra a invariante anti-merge-AUTOMÁTICO (FR-16). merge-base
atualiza o BRANCH DA TAREFA com o drift da base; o merge do PR na base continua
100% humano. São coisas diferentes: aqui o merge é origin/main → branch da
tarefa (trazer o drift PARA a tarefa), não branch da tarefa → main.

`orphaned_threads` é a asserção de exit da Fase 4: DEVE ser 0 no caminho
merge-base. Medido comparando os shas ancorados ANTES/DEPOIS: uma thread está
órfã se o commit em que ela foi ancorada deixou de ser alcançável a partir do
tip do branch (merge-base --is-ancestor). merge preserva a alcançabilidade;
rebase a quebra (o teste negativo documenta isso).

Contra git REAL (bare repo local + clones, como o git_checkpoint do WS-C) —
nenhum mock para as garantias de durabilidade/segurança (P8).
"""
from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

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
        super().__init__(f"git {' '.join(argv)} falhou (exit={returncode}): {stderr.strip()}")


def _git(
    workspace_dir: str, *args: str, check: bool = True, timeout: int = 120
) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args],
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
    # checkout do branch da tarefa (idempotente — já pode estar nele).
    proc = _git(workspace_dir, "rev-parse", "--verify", "--quiet", branch, check=False)
    if proc.returncode == 0:
        _git(workspace_dir, "checkout", "-q", branch)


def is_commit_reachable(workspace_dir: str, sha: str, branch: str) -> bool:
    """True se `sha` é alcançável a partir do tip de `branch` (é um ancestral
    ou o próprio tip). É exatamente o que decide se uma thread de review
    ancorada em `sha` continua "viva" (não-órfã): o GitHub mantém a âncora
    enquanto o commit faz parte da história do PR."""
    proc = _git(workspace_dir, "merge-base", "--is-ancestor", sha, branch, check=False)
    return proc.returncode == 0


def count_orphaned_threads(
    workspace_dir: str, branch: str, anchored_shas: Sequence[str]
) -> list[str]:
    """Retorna os shas ancorados que ficaram ÓRFÃOS (não mais alcançáveis a
    partir do tip do branch). merge-base preserva todos → lista vazia; rebase
    reescreve os commits → todos os shas originais viram órfãos."""
    orphaned: list[str] = []
    for sha in anchored_shas:
        if not sha:
            continue
        # commit ainda existe no repo? (rebase não deleta o objeto imediatamente,
        # mas ele deixa de ser alcançável — que é o que orfaneia a thread.)
        if not is_commit_reachable(workspace_dir, sha, branch):
            orphaned.append(sha)
    return orphaned


def _fetch_base(workspace_dir: str, base_branch: str) -> str:
    """Busca o tip atual da base no origin e retorna o sha (via FETCH_HEAD)."""
    _git(workspace_dir, "fetch", "--quiet", "origin", base_branch)
    return _current_sha(workspace_dir, "FETCH_HEAD")


def _has_drift(workspace_dir: str, branch: str) -> bool:
    """A base avançou além do que o branch já contém? Conta os commits em
    FETCH_HEAD que NÃO são alcançáveis a partir do branch."""
    out = _git(workspace_dir, "rev-list", "--count", f"{branch}..FETCH_HEAD").stdout.strip()
    return int(out or "0") > 0


class MergeBaseConfig:
    """Localização do repo git para o wrapper de Activity (seam de integração
    com o WS-C — os testes chamam o core diretamente com paths explícitos, como
    o LocalFakeSandbox faz para o L1). Em produção o `workspace_dir` é o
    workspace da tarefa do sandbox do WS-C e o remote `origin` é o repo real do
    GitHub (URL autenticada da GitHub App)."""

    def __init__(self) -> None:
        self.git_root = os.environ.get(
            "DSE_WSE_GIT_ROOT",
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "merge_base_repos"),
        )

    def locations(self, work_item_id: str) -> tuple[str, str]:
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
    """Atualiza `branch` com o drift de `base_branch` SEM reescrever história
    quando há (ou já houve) review humano. `workspace_dir` é um clone/checkout
    do repo com o `origin` remote configurado. Ver docstring do módulo."""
    anchored = [s for s in anchored_review_shas if s]
    _ensure_on_branch(workspace_dir, branch)
    old_tip = _current_sha(workspace_dir, "HEAD")
    base_tip = _fetch_base(workspace_dir, base_branch)

    # 1) Sem drift → noop. Nada a mesclar, nada a orfanar.
    if not _has_drift(workspace_dir, branch):
        return _finish(
            work_item_id=work_item_id, tenant_id=tenant_id, repo=repo, branch=branch,
            base_branch=base_branch, strategy="noop_no_drift", conflict=False,
            orphaned=[], anchored=anchored, first_human_review_done=first_human_review_done,
            old_tip=old_tip, new_tip=old_tip,
            detail=f"base {base_branch}@{base_tip[:8]} já contido no branch — nada a fazer",
            actor=actor, record=record,
        )

    # 2) Escolha de estratégia — DETERMINÍSTICA (P1). Rebase é permitido SÓ
    #    antes do 1º review humano E quando não há NENHUMA thread ancorada
    #    (belt-and-suspenders: mesmo com first_human_review_done=False, se já
    #    existem threads ancoradas, jamais rebase — orfanaria).
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
                detail="conflito no rebase pré-review — abortado; escalar a humano",
                actor=actor, record=record,
            )
        strategy = "rebase_prefirst_review"
        force = True
    else:
        # git merge origin/main NO branch da tarefa. Preserva a história →
        # preserva as âncoras das threads → zero órfãs.
        proc = _git(workspace_dir, "merge", "--no-edit", "FETCH_HEAD", check=False)
        if proc.returncode != 0:
            _git(workspace_dir, "merge", "--abort", check=False)
            return _finish(
                work_item_id=work_item_id, tenant_id=tenant_id, repo=repo, branch=branch,
                base_branch=base_branch, strategy="merge_base", conflict=True,
                orphaned=[], anchored=anchored, first_human_review_done=first_human_review_done,
                old_tip=old_tip, new_tip=old_tip,
                detail="conflito de merge não-resolvível — abortado; workflow escala a humano "
                       "(o agente NUNCA resolve à força)",
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
        detail=f"atualizado por {strategy}: {old_tip[:8]} -> {new_tip[:8]} "
               f"(base {base_tip[:8]}); órfãs={len(orphaned)}",
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
