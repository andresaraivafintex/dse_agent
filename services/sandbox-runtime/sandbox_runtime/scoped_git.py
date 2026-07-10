"""Git de escopo limitado para a sessão Coder (WSC-E3-T2).

Duas camadas de enforcement, independentes uma da outra:

1. **Toolset** — `ScopedGitSession` é a ÚNICA forma pela qual o código do
   sandbox (a Activity `run_coder_turn`, nunca o LLM/substrato diretamente —
   ver `activities.py`) grava no git. Ela expõe apenas `.commit()` e
   `.push()`; `.push()` tem o refspec (`HEAD:refs/heads/<branch>`)
   *hardcoded* — não existe parâmetro para passar `--force` ou outro branch.
   Não existe nenhum método `run_git_command(*args)` de escape, nem
   `open_pull_request()`. O LLM nunca recebe uma ferramenta de git — ele só
   edita arquivos no workspace (P1: nenhuma decisão de fluxo por LLM).

2. **Escopo do remoto (server-side)** — o "origin" de checkpoint (bare repo
   local que simula o remoto de verdade nesta fase, ver `git_checkpoint.py`)
   tem um hook real `pre-receive` instalado (`install_pre_receive_guard`) que
   recusa: (a) qualquer ref que não seja o branch permitido da tarefa, (b)
   qualquer update non-fast-forward (force-push). Isso é reforçado mesmo se
   alguém contornar `ScopedGitSession` e rodar `git push --force` cru — o
   hook roda no lado do "servidor" (o bare repo), não no lado do cliente, e
   portanto vale independentemente do código que fez o push.

   Em produção (push real para GitHub via egress-proxy) o equivalente é o
   escopo do token do GitHub App injetado pelo proxy (`egress_proxy.credentials
   .ScopedCredential`) — o token nunca tem a permissão `pull_requests:write`,
   então uma tentativa de `gh pr create`/`POST /repos/.../pulls` feita de
   dentro do sandbox falha por falta de permissão do próprio token, não por
   "boa vontade" do código. `ScopedCredential.create_pull_request()` (ver
   `egress_proxy/credentials.py`) modela isso mesmo no modo fixture local.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

PRE_RECEIVE_HOOK_TEMPLATE = """#!/usr/bin/env python3
import sys

ALLOWED_REF = "refs/heads/{branch}"

def main():
    rejected = False
    for line in sys.stdin:
        old_sha, new_sha, refname = line.strip().split()
        if refname != ALLOWED_REF:
            sys.stderr.write(
                f"dse-scope: recusado — ref {{refname}} fora do branch permitido "
                f"{{ALLOWED_REF}}\\n"
            )
            rejected = True
            continue
        is_force = (
            old_sha != "0" * 40
            and new_sha != "0" * 40
            and not _is_fast_forward(old_sha, new_sha)
        )
        if is_force:
            sys.stderr.write(
                "dse-scope: recusado — non-fast-forward (force-push) bloqueado "
                "pelo escopo da tarefa\\n"
            )
            rejected = True
    if rejected:
        sys.exit(1)
    sys.exit(0)


def _is_fast_forward(old_sha, new_sha):
    import subprocess as sp

    try:
        merge_base = sp.check_output(
            ["git", "merge-base", "--is-ancestor", old_sha, new_sha]
        )
        return True
    except sp.CalledProcessError:
        return False


if __name__ == "__main__":
    main()
"""


class GitScopeViolation(Exception):
    """Levantado quando uma operação git tenta sair do escopo da tarefa."""


def install_pre_receive_guard(bare_repo_path: str, allowed_branch: str) -> None:
    """Instala um hook `pre-receive` real no bare repo de checkpoint,
    recusando pushes fora do branch da tarefa ou non-fast-forward (force).
    Idempotente — sobrescreve o hook existente."""
    hooks_dir = Path(bare_repo_path) / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / "pre-receive"
    hook_path.write_text(PRE_RECEIVE_HOOK_TEMPLATE.format(branch=allowed_branch))
    hook_path.chmod(0o755)


@dataclass
class ScopedGitSession:
    """Única superfície de escrita em git disponível para a Activity
    `run_coder_turn`. Não expõe force-push, não expõe criação de PR, não
    expõe um `run(*args)` genérico."""

    workspace_dir: str
    branch: str
    remote_name: str = "origin"

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=self.workspace_dir,
            check=True,
            capture_output=True,
            text=True,
        )

    def ensure_identity(self, name: str = "dse-coder", email: str = "coder@dse.local") -> None:
        self._run(["config", "user.name", name])
        self._run(["config", "user.email", email])

    def has_changes(self) -> bool:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.workspace_dir,
            capture_output=True,
            text=True,
        )
        return bool(result.stdout.strip())

    def commit(self, message: str) -> str:
        self._run(["add", "-A"])
        self._run(["commit", "-m", message, "--allow-empty"])
        sha = self._run(["rev-parse", "HEAD"]).stdout.strip()
        return sha

    def push(self) -> None:
        """Push hardcoded para `HEAD:refs/heads/<branch>` no remoto
        configurado — não há forma de passar `--force` ou outro refspec por
        esta API. Falhas do hook `pre-receive` do lado do servidor propagam
        como `subprocess.CalledProcessError` (P6: falha limpa, não engolida)."""
        try:
            self._run(["push", self.remote_name, f"HEAD:refs/heads/{self.branch}"])
        except subprocess.CalledProcessError as e:
            raise GitScopeViolation(
                f"push recusado pelo remoto (escopo): {e.stderr}"
            ) from e

    def current_sha(self) -> str:
        return self._run(["rev-parse", "HEAD"]).stdout.strip()

    def files_changed_against(self, base_sha: str) -> list[str]:
        result = self._run(["diff", "--name-only", base_sha, "HEAD"])
        return [line for line in result.stdout.splitlines() if line]


# Assinatura de "conjunto de ferramentas seguro": usado pelo teste adversarial
# para provar que a API pública de ScopedGitSession não contém nenhum escape
# hatch de force-push / PR / comando genérico.
FORBIDDEN_METHOD_NAMES = {
    "force_push",
    "push_force",
    "create_pull_request",
    "open_pr",
    "run_git_command",
    "run",
    "exec",
}
