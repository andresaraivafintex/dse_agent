"""Cliente mínimo da API REST do GitHub usado pelo PR finalizer (WSE-E3),
pelo backend de comentário mutável (WSE-E3-T7) e pelo consumo de status de CI
(WSE-E4-T9a).

Duas implementações do mesmo `Protocol` `GitHubClient`:

  - `RealGitHubClient` — chamadas HTTP reais à API do GitHub, autenticado como
    GitHub App (via `app_auth.py`). É o que roda em produção.
  - `FakeGitHubClient` — fixture in-memory, usada em todos os testes deste
    workstream porque nenhuma GitHub App real está registrada nesta sessão
    (falta `GITHUB_APP_ID`/`GITHUB_APP_PRIVATE_KEY`/`GITHUB_APP_INSTALLATION_ID`
    reais — ver README). Implementa a MESMA interface, então a lógica de
    idempotência do PR finalizer é testada de verdade; só o transporte HTTP é
    substituído.

`build_github_client()` escolhe automaticamente: real se as 3 env vars de
GitHub App estiverem presentes, fake caso contrário (nunca falha silenciosamente
— loga qual modo escolheu).
"""
from __future__ import annotations

import itertools
import logging
import time
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from dse_validation.config import GitHubConfig
from dse_validation.github.app_auth import fetch_installation_token

logger = logging.getLogger("dse_validation.github")


class GitHubClient(Protocol):
    def get_open_pr_for_branch(self, repo: str, branch: str) -> dict | None: ...

    def create_pr(self, repo: str, head: str, base: str, title: str, body: str) -> dict: ...

    def post_issue_comment(self, repo: str, issue_number: int, body: str) -> str: ...

    def edit_issue_comment(self, repo: str, comment_id: str, body: str) -> None: ...

    def list_check_runs(self, repo: str, ref: str) -> list[dict]: ...

    def authenticated_remote_url(self, repo: str) -> str:
        """URL https://x-access-token:<token>@github.com/<repo>.git para git push
        autenticado como a GitHub App, sem expor o token em nenhum argv de log."""
        ...


# ---------------------------------------------------------------------------
# Real
# ---------------------------------------------------------------------------
class RealGitHubClient:
    def __init__(self, cfg: GitHubConfig):
        self._cfg = cfg
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    def _installation_token(self) -> str:
        # margem de 60s antes da expiração declarada (1h) para evitar corrida.
        if self._token is None or time.time() >= self._token_expires_at - 60:
            self._token = fetch_installation_token(
                self._cfg.app_id, self._cfg.private_key_pem, self._cfg.installation_id, self._cfg.api_base_url
            )
            self._token_expires_at = time.time() + 3600
        return self._token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._installation_token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def get_open_pr_for_branch(self, repo: str, branch: str) -> dict | None:
        owner = repo.split("/")[0]
        resp = httpx.get(
            f"{self._cfg.api_base_url}/repos/{repo}/pulls",
            headers=self._headers(),
            params={"head": f"{owner}:{branch}", "state": "open"},
            timeout=15.0,
        )
        resp.raise_for_status()
        items = resp.json()
        if not items:
            return None
        pr = items[0]
        return {"number": pr["number"], "html_url": pr["html_url"], "state": pr["state"]}

    def create_pr(self, repo: str, head: str, base: str, title: str, body: str) -> dict:
        resp = httpx.post(
            f"{self._cfg.api_base_url}/repos/{repo}/pulls",
            headers=self._headers(),
            json={"title": title, "head": head, "base": base, "body": body},
            timeout=15.0,
        )
        resp.raise_for_status()
        pr = resp.json()
        return {"number": pr["number"], "html_url": pr["html_url"], "state": pr["state"]}

    def post_issue_comment(self, repo: str, issue_number: int, body: str) -> str:
        resp = httpx.post(
            f"{self._cfg.api_base_url}/repos/{repo}/issues/{issue_number}/comments",
            headers=self._headers(),
            json={"body": body},
            timeout=15.0,
        )
        resp.raise_for_status()
        return str(resp.json()["id"])

    def edit_issue_comment(self, repo: str, comment_id: str, body: str) -> None:
        resp = httpx.patch(
            f"{self._cfg.api_base_url}/repos/{repo}/issues/comments/{comment_id}",
            headers=self._headers(),
            json={"body": body},
            timeout=15.0,
        )
        resp.raise_for_status()

    def list_check_runs(self, repo: str, ref: str) -> list[dict]:
        resp = httpx.get(
            f"{self._cfg.api_base_url}/repos/{repo}/commits/{ref}/check-runs",
            headers=self._headers(),
            timeout=15.0,
        )
        resp.raise_for_status()
        return resp.json().get("check_runs", [])

    def authenticated_remote_url(self, repo: str) -> str:
        token = self._installation_token()
        return f"https://x-access-token:{token}@github.com/{repo}.git"


# ---------------------------------------------------------------------------
# Fake / modo local — sem GitHub App real. Documentado como fixture no README.
# ---------------------------------------------------------------------------
@dataclass
class FakeGitHubClient:
    """Estado em memória que imita o GitHub o suficiente para testar a lógica
    de idempotência do PR finalizer, o backend de comentário e o consumo de
    status de CI SEM rede/credenciais reais. Cada instância representa o
    "estado do GitHub" de forma persistente entre chamadas (ao contrário do
    nosso Postgres, que pode ser "esquecido" simulando um crash)."""

    _prs: dict[tuple[str, str], dict] = field(default_factory=dict)  # (repo, branch) -> pr dict
    _pr_by_number: dict[tuple[str, int], dict] = field(default_factory=dict)
    _comments: dict[str, str] = field(default_factory=dict)  # comment_id -> body
    _check_runs: dict[tuple[str, str], list[dict]] = field(default_factory=dict)
    _comment_id_seq: itertools.count = field(default_factory=lambda: itertools.count(1))
    _pr_number_seq: itertools.count = field(default_factory=lambda: itertools.count(100))
    create_pr_calls: int = 0

    def get_open_pr_for_branch(self, repo: str, branch: str) -> dict | None:
        pr = self._prs.get((repo, branch))
        if pr is None or pr["state"] != "open":
            return None
        return dict(pr)

    def create_pr(self, repo: str, head: str, base: str, title: str, body: str) -> dict:
        self.create_pr_calls += 1
        number = next(self._pr_number_seq)
        pr = {
            "number": number,
            "html_url": f"https://github.com/{repo}/pull/{number}",
            "state": "open",
            "title": title,
            "body": body,
            "base": base,
        }
        self._prs[(repo, head)] = pr
        self._pr_by_number[(repo, number)] = pr
        return dict(pr)

    def post_issue_comment(self, repo: str, issue_number: int, body: str) -> str:
        comment_id = str(next(self._comment_id_seq))
        self._comments[comment_id] = body
        return comment_id

    def edit_issue_comment(self, repo: str, comment_id: str, body: str) -> None:
        if comment_id not in self._comments:
            raise KeyError(f"comment {comment_id} não existe no FakeGitHubClient")
        self._comments[comment_id] = body

    def list_check_runs(self, repo: str, ref: str) -> list[dict]:
        return list(self._check_runs.get((repo, ref), []))

    def set_check_runs(self, repo: str, ref: str, runs: list[dict]) -> None:
        """Só para teste — popula os check-runs "reportados pelo CI"."""
        self._check_runs[(repo, ref)] = runs

    def authenticated_remote_url(self, repo: str) -> str:
        return f"https://x-access-token:fake-local-token@github.com/{repo}.git"


def build_github_client(cfg: GitHubConfig | None = None) -> GitHubClient:
    cfg = cfg or GitHubConfig()
    if cfg.is_configured:
        logger.info("dse_validation: usando RealGitHubClient (GitHub App configurada)")
        return RealGitHubClient(cfg)
    logger.warning(
        "dse_validation: GITHUB_APP_ID/GITHUB_APP_PRIVATE_KEY/GITHUB_APP_INSTALLATION_ID "
        "não configurados — usando FakeGitHubClient (modo local/teste, NÃO produção)"
    )
    return FakeGitHubClient()
