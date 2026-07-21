"""WSE-E4-T10 — repo git de manifests de preview (o "GitOps source of truth").

O repo BARE vive em `services/validation/preview_repo/preview-manifests.git`
(bind mount read-only no container nginx `dse-wse-gitserver`, que o serve ao
Argo CD do cluster k3d via git dumb-HTTP — rede `dse_net`). O host escreve
direto pelo filesystem (clone temporário -> commit -> push para o path) e roda
`git update-server-info` (exigência do dumb protocol) após cada push.

Criar/remover um preview É uma operação de git (GitOps de verdade): o
ApplicationSet do Argo CD (generator git `previews/*`) materializa/poda a
Application correspondente. Por isso o TTL reaper também opera AQUI (remove o
diretório do git) — deletar só o namespace no cluster seria revertido pelo
selfHeal do Argo CD.
"""
from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger("dse_validation.preview.gitops")

REPO_BARE_NAME = "preview-manifests.git"


def _run(args: list[str], cwd: str | Path, timeout: int = 60) -> subprocess.CompletedProcess:
    proc = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} falhou (exit={proc.returncode}): {proc.stderr.strip()}")
    return proc


def bare_repo_path(repo_dir: str | Path) -> Path:
    return Path(repo_dir) / REPO_BARE_NAME


def ensure_repo(repo_dir: str | Path) -> Path:
    """Idempotente: cria o repo bare (com commit inicial vazio + branch `main`)
    e o deixa servível via dumb HTTP (`update-server-info`)."""
    bare = bare_repo_path(repo_dir)
    if (bare / "HEAD").is_file():
        return bare
    bare.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "--bare", "-b", "main", "-q", str(bare)], cwd=bare.parent)
    with tempfile.TemporaryDirectory(prefix="dse-preview-init-") as tmp:
        _run(["git", "clone", "-q", str(bare), "work"], cwd=tmp)
        work = Path(tmp) / "work"
        _run(["git", "config", "user.email", "dse-validation@dse.local"], cwd=work)
        _run(["git", "config", "user.name", "DSE Validation"], cwd=work)
        _run(["git", "checkout", "-q", "-b", "main"], cwd=work)
        (work / "README.md").write_text(
            "# preview-manifests\n\nGerado por dse_validation (WSE-E4-T10). Nao editar a mao.\n"
        )
        _run(["git", "add", "-A"], cwd=work)
        _run(["git", "commit", "-q", "-m", "init preview-manifests"], cwd=work)
        _run(["git", "push", "-q", "origin", "main"], cwd=work)
    _run(["git", "update-server-info"], cwd=bare)
    logger.info("preview gitops: repo bare inicializado em %s", bare)
    return bare


def _commit_and_push(work: Path, message: str, bare: Path) -> str:
    _run(["git", "add", "-A"], cwd=work)
    status = _run(["git", "status", "--porcelain"], cwd=work)
    if not status.stdout.strip():
        sha = _run(["git", "rev-parse", "HEAD"], cwd=work).stdout.strip()
        return sha  # no-op idempotente
    _run(["git", "commit", "-q", "-m", message], cwd=work)
    _run(["git", "push", "-q", "origin", "main"], cwd=work)
    _run(["git", "update-server-info"], cwd=bare)
    return _run(["git", "rev-parse", "HEAD"], cwd=work).stdout.strip()


def write_preview_dir(repo_dir: str | Path, preview_name: str, manifests: dict[str, str]) -> str:
    """Escreve/atualiza `previews/<preview_name>/<arquivo>` e faz push.
    Retorna o commit sha. Idempotente (conteúdo igual => no-op)."""
    bare = ensure_repo(repo_dir)
    with tempfile.TemporaryDirectory(prefix="dse-preview-push-") as tmp:
        _run(["git", "clone", "-q", "-b", "main", str(bare), "work"], cwd=tmp)
        work = Path(tmp) / "work"
        _run(["git", "config", "user.email", "dse-validation@dse.local"], cwd=work)
        _run(["git", "config", "user.name", "DSE Validation"], cwd=work)
        target = work / "previews" / preview_name
        target.mkdir(parents=True, exist_ok=True)
        for filename, content in manifests.items():
            (target / filename).write_text(content)
        return _commit_and_push(work, f"preview {preview_name}: upsert", bare)


def remove_preview_dir(repo_dir: str | Path, preview_name: str) -> str:
    """Remove `previews/<preview_name>/` (GitOps delete — o ApplicationSet poda
    a Application e o finalizer cascateia os recursos). Idempotente."""
    bare = ensure_repo(repo_dir)
    with tempfile.TemporaryDirectory(prefix="dse-preview-rm-") as tmp:
        _run(["git", "clone", "-q", "-b", "main", str(bare), "work"], cwd=tmp)
        work = Path(tmp) / "work"
        _run(["git", "config", "user.email", "dse-validation@dse.local"], cwd=work)
        _run(["git", "config", "user.name", "DSE Validation"], cwd=work)
        target = work / "previews" / preview_name
        if target.is_dir():
            _run(["git", "rm", "-r", "-q", f"previews/{preview_name}"], cwd=work)
        return _commit_and_push(work, f"preview {preview_name}: remove (TTL/reap)", bare)


def list_preview_dirs(repo_dir: str | Path) -> list[str]:
    bare = ensure_repo(repo_dir)
    proc = subprocess.run(
        ["git", "ls-tree", "--name-only", "main", "previews/"],
        cwd=str(bare), capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return []
    return [line.split("/", 1)[1] for line in proc.stdout.strip().splitlines() if "/" in line]
