"""S4 (Fase 5): clone do repositório-alvo REAL no workspace do sandbox.

Decisão de arquitetura (risco #3 do plano de integração): o clone acontece no
CONTROL PLANE (a Activity `provision_sandbox` roda no orchestrator, confiável),
com um installation token de GitHub App minto aqui e IMEDIATAMENTE removido do
`git config` — o workspace resultante contém o CÓDIGO real, nunca o token. O
sandbox monta esse workspace; a invariante "nenhuma credencial no sandbox"
(P2/ADR-12) é preservada porque o token só existe em memória do control plane
durante o clone e nunca é escrito de forma persistente.

O caminho de produção (egress-proxy injetando a credencial na borda para o
próprio sandbox clonar) tem um gap conhecido em CONNECT-tunnel (Fase 3); este
clone-no-control-plane é o caminho de PoC honesto e explicitado.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

_GITHUB_API = os.environ.get("GITHUB_API_BASE_URL", "https://api.github.com")


class RepoCloneError(RuntimeError):
    pass


def _run(args: list[str], cwd: str | None = None) -> str:
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        # NUNCA logar o comando cru (pode conter o token na URL) — só o stderr.
        raise RepoCloneError(f"git falhou (exit={r.returncode}): {r.stderr[-300:]}")
    return r.stdout


def mint_installation_token() -> str | None:
    """Minta um installation access token curto a partir das credenciais de
    GitHub App no ambiente do control plane (GITHUB_APP_ID/PRIVATE_KEY/
    INSTALLATION_ID — as mesmas que o orchestrator já injeta do Vault).
    Retorna None se não configurado (modo fixture/teste)."""
    app_id = os.environ.get("GITHUB_APP_ID")
    private_key = os.environ.get("GITHUB_APP_PRIVATE_KEY")
    installation_id = os.environ.get("GITHUB_APP_INSTALLATION_ID")
    if not (app_id and private_key and installation_id):
        return None
    import jwt  # PyJWT — só quando realmente configurado
    import httpx

    now = int(time.time())
    app_jwt = jwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": app_id},
        private_key, algorithm="RS256",
    )
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(
            f"{_GITHUB_API}/app/installations/{installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {app_jwt}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
        )
        resp.raise_for_status()
        # GitHub retorna o installation token na chave "token".
        return resp.json()["token"]


def clone_repo_into(
    *, workspace_dir: str, repo: str, base_branch: str, task_branch: str,
    bare_repo_path: str, token: str | None,
) -> bool:
    """Clona `github.com/<repo>` (base_branch) no workspace, cria o branch da
    tarefa a partir dele, re-aponta `origin` para o bare repo local (destino
    dos checkpoints) e faz push. Retorna True se clonou o repo real; False se
    não havia token (o caller cai para o init de workspace vazio).

    O token entra SÓ na URL de clone e é apagado logo após (set-url para o bare
    repo) — verificável: `.git/config` do workspace não contém o token."""
    if not token:
        return False
    Path(workspace_dir).parent.mkdir(parents=True, exist_ok=True)
    if Path(workspace_dir).exists():
        # provisionamento idempotente: se já clonado, não repetir
        return True
    host = _GITHUB_API.replace("https://api.", "").replace("/", "") or "github.com"
    if host == "github.com":  # api.github.com -> github.com
        host = "github.com"
    clone_url = f"https://x-access-token:{token}@github.com/{repo}.git"
    _run(["git", "clone", "--branch", base_branch, "--depth", "50", clone_url, workspace_dir])
    # SCRUB: remove a URL tokenizada do config imediatamente, apontando origin
    # para o bare repo local (checkpoints). O token não persiste em lugar nenhum.
    _run(["git", "remote", "set-url", "origin", bare_repo_path], cwd=workspace_dir)
    _run(["git", "checkout", "-b", task_branch], cwd=workspace_dir)
    from .scoped_git import ScopedGitSession  # reuso da sessão de escopo (git de escopo limitado)
    session = ScopedGitSession(workspace_dir=workspace_dir, branch=task_branch)
    session.ensure_identity()
    (Path(workspace_dir) / ".dse-task-branch").write_text(task_branch)
    session.push()
    return True


def token_absent_from_config(workspace_dir: str) -> bool:
    """Prova da invariante: o token não está no git config do workspace."""
    cfg = Path(workspace_dir) / ".git" / "config"
    if not cfg.exists():
        return True
    return "x-access-token" not in cfg.read_text()
