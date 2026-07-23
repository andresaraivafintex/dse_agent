"""Higiene determinística pós-turno do workspace (P1) — módulo VENDORÁVEL.

Extraído de `activities.py` (Fase 1, plano 09) para poder rodar em DOIS
lugares com uma única fonte de verdade: no worker (runtime Docker, git pelo
bind mount) e dentro do agent-runner (runtime K8s, git in-pod via
`--op post_turn`). Por isso as dependências são só stdlib + `dse_contracts`
(ambos presentes na imagem do runner) — nada de docker/temporal/audit aqui;
quem audita é o chamador, com as listas retornadas.

As três operações e as histórias reais que as originaram:
  - `prune_disposable_artifacts`: CLI cria relatórios espontâneos
    (BUG_FIX_REPORT.md) — apaga SÓ lixo descartável novo; fonte nova legítima
    fora do plano sobrevive (expected_files é advisory desde 2026-07-22).
  - `restore_lockfile_churn`: npm reescreveu 16 linhas de package-lock.json
    sem mudança no manifesto par e o diff_budget reprovou a tarefa.
  - `revert_test_edits`: o Coder editou seed compartilhado de teste e quebrou
    teste irmão — testes são do Tester; qualquer edição de teste do Coder é
    revertida ao estado do início do turno.
"""
from __future__ import annotations

import logging
import os
import subprocess

from dse_contracts.paths import is_disposable_artifact, is_test_path, lockfile_manifest_for

logger = logging.getLogger("sandbox_runtime.workspace_hygiene")


def prune_disposable_artifacts(
    workspace_dir: str, expected_files: list[str], work_item_id: str
) -> tuple[list[str], list[str]]:
    """Apaga arquivos NOVOS (untracked) que são LIXO óbvio do CLI antes do
    commit. Nunca toca: o que o plano pediu, testes, ou o demo do work item.
    Best-effort (o L1 é o gate duro). Retorna `(pruned, kept_out_of_plan)`."""
    try:
        # `-uall`: lista CADA untracked individualmente (sem colapsar diretório
        # novo em `?? src/`); .gitignore continua respeitado.
        porcelain = subprocess.run(
            ["git", "status", "--porcelain", "-uall"], cwd=workspace_dir,
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:  # noqa: BLE001 — prune é best-effort; o L1 é o gate duro
        logger.warning("prune pós-coder falhou (git status); o L1 segue como gate")
        return [], []

    expected = set(expected_files)
    pruned: list[str] = []
    kept_out_of_plan: list[str] = []
    for line in porcelain.splitlines():
        if not line.startswith("??"):
            continue
        rel = line[3:].strip().strip('"')
        if rel in expected or is_test_path(rel) or rel.startswith(f"demos/{work_item_id}"):
            continue
        if not is_disposable_artifact(rel):
            kept_out_of_plan.append(rel)  # fonte nova legítima fora do plano — FICA
            continue
        try:
            os.remove(os.path.join(workspace_dir, rel))
            pruned.append(rel)
        except OSError:
            pass
    return pruned, kept_out_of_plan


def restore_lockfile_churn(workspace_dir: str) -> list[str]:
    """Lockfile mudou mas o manifesto par (package.json…) NÃO mudou →
    restaura (modificado) ou remove (novo). Retorna os paths tratados."""
    try:
        porcelain = subprocess.run(
            ["git", "status", "--porcelain"], cwd=workspace_dir,
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:  # noqa: BLE001 — best-effort; o L1 tem a mesma isenção
        return []
    status_by_path: dict[str, str] = {}
    for line in porcelain.splitlines():
        if len(line) > 3:
            status_by_path[line[3:].strip().strip('"')] = line[:2]
    restored: list[str] = []
    for rel, st in sorted(status_by_path.items()):
        manifest = lockfile_manifest_for(rel)
        if manifest is None or manifest in status_by_path:
            continue
        try:
            if st == "??":
                os.remove(os.path.join(workspace_dir, rel))
                restored.append(rel)
            else:
                proc = subprocess.run(
                    ["git", "checkout", "--", rel], cwd=workspace_dir,
                    capture_output=True, text=True, timeout=30,
                )
                if proc.returncode == 0:
                    restored.append(rel)
        except OSError:
            continue
    return restored


def revert_test_edits(workspace_dir: str, turn_start_sha: str) -> list[str]:
    """Reverte QUALQUER mudança do Coder em test paths ao estado do início do
    turno: edição/remoção → `git checkout <sha>`; novo (untracked) → remove.
    Best-effort. Retorna os paths revertidos."""
    try:
        porcelain = subprocess.run(
            ["git", "status", "--porcelain", "-uall"], cwd=workspace_dir,
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:  # noqa: BLE001 — best-effort
        logger.warning("revert de testes do coder falhou (git status)")
        return []

    reverted: list[str] = []
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        status, rel = line[:2], line[3:].strip().strip('"')
        if "->" in rel:  # rename: "old -> new" — pega o destino
            rel = rel.split("->")[-1].strip()
        if not is_test_path(rel):
            continue
        try:
            if status.strip() == "??":
                os.remove(os.path.join(workspace_dir, rel))
                reverted.append(rel)
            else:
                proc = subprocess.run(
                    ["git", "checkout", turn_start_sha, "--", rel], cwd=workspace_dir,
                    capture_output=True, text=True, timeout=30,
                )
                if proc.returncode == 0:
                    reverted.append(rel)
        except OSError:
            continue
    return reverted
