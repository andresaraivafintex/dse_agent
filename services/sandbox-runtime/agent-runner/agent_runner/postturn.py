"""Op `--op post_turn` — pós-turno do Coder DENTRO do sandbox (runtime K8s).

Mesma sequência determinística que o worker executa no runtime Docker
(`_run_coder_turn_impl`), com a mesma fonte de verdade (`workspace_hygiene` e
`scoped_git`, vendorados na imagem): prune de descartáveis → restore de
lockfile churn → revert de edições de teste → commit/push escopado (refspec
fixo, hook pre-receive no remoto). Devolve as listas para o worker auditar
(P8 fica no worker; aqui não há Postgres nem credencial de audit).
"""
from __future__ import annotations

from dse_contracts import PostTurnRequest, PostTurnResult

try:  # dev/test: pacotes do worker no venv
    from sandbox_runtime.scoped_git import ScopedGitSession
    from sandbox_runtime.workspace_hygiene import (
        prune_disposable_artifacts,
        restore_lockfile_churn,
        revert_test_edits,
    )
except ImportError:  # imagem: cópias vendoradas no build (Dockerfile)
    from ._scoped_git import ScopedGitSession  # type: ignore[no-redef]
    from ._workspace_hygiene import (  # type: ignore[no-redef]
        prune_disposable_artifacts,
        restore_lockfile_churn,
        revert_test_edits,
    )

from .gitops import _ensure_safe_directory


def run_post_turn(req: PostTurnRequest) -> PostTurnResult:
    try:
        _ensure_safe_directory()

        pruned: list[str] = []
        kept: list[str] = []
        if req.expected_files:
            pruned, kept = prune_disposable_artifacts(
                req.workspace_dir, req.expected_files, req.work_item_id
            )
        restored = restore_lockfile_churn(req.workspace_dir)
        reverted = revert_test_edits(req.workspace_dir, req.turn_start_sha)

        session = ScopedGitSession(workspace_dir=req.workspace_dir, branch=req.branch)
        session.ensure_identity()
        if session.has_changes():
            session.commit(req.commit_message)
        session.push()
        sha = session.current_sha()
        files_changed = (
            session.files_changed_against(req.turn_start_sha)
            if sha != req.turn_start_sha
            else []
        )
        return PostTurnResult(
            sha=sha,
            files_changed=files_changed,
            pruned=pruned,
            kept_out_of_plan=kept,
            restored_lockfiles=restored,
            reverted_tests=reverted,
        )
    except Exception as exc:  # noqa: BLE001 — P6 (inclui GitScopeViolation)
        return PostTurnResult(
            error=f"{type(exc).__name__}: {str(exc)[:400]}",
            error_kind="gitops_error",
        )
