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

    def get_pull_request(self, repo: str, pr_number: int) -> dict | None: ...

    def create_pr(self, repo: str, head: str, base: str, title: str, body: str) -> dict: ...

    def post_issue_comment(self, repo: str, issue_number: int, body: str) -> str: ...

    def edit_issue_comment(self, repo: str, comment_id: str, body: str) -> None: ...

    def list_check_runs(self, repo: str, ref: str) -> list[dict]: ...

    def rerequest_check_run(self, repo: str, check_run_id: int) -> bool:
        """Fase 3 (WSE-E4-T9b) — targeted re-run de UM check-run (fix commit).
        `POST /repos/{repo}/check-runs/{id}/rerequest`. Retorna False quando o
        CI do repo NÃO suporta re-run por job (403/422 da API) — o caller
        registra e segue (nunca bloqueia por isso)."""
        ...

    def list_review_threads(self, repo: str, pr_number: int) -> list[dict]:
        """Fase 4 (WSE-E6-T16) — threads de review do PR, cada uma ANCORADA em
        um commit. `GET /repos/{repo}/pulls/{n}/comments` (review comments).
        Retorna `[{id, commit_id, original_commit_id, path}]`. O
        `original_commit_id` é o sha em que a thread foi ancorada — é ele que
        rebase+force-push orfanaria (merge-base preserva). Usado pelo wrapper de
        Activity para saber quais shas NÃO podem sumir da história do branch."""
        ...

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

    def get_pull_request(self, repo: str, pr_number: int) -> dict | None:
        """Plano 08 §F (F1) — estado REAL do PR na fonte (GitHub), para verificar
        um signal de merge contra a verdade e não só contra o envelope. Retorna
        merged/merged_by/merge_commit_sha/head_sha; None se o PR não existe."""
        resp = httpx.get(
            f"{self._cfg.api_base_url}/repos/{repo}/pulls/{pr_number}",
            headers=self._headers(),
            timeout=15.0,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        pr = resp.json()
        return {
            "number": pr["number"],
            "state": pr["state"],
            "merged": bool(pr.get("merged")),
            "merged_by": (pr.get("merged_by") or {}).get("login"),
            "merge_commit_sha": pr.get("merge_commit_sha"),
            "head_sha": (pr.get("head") or {}).get("sha"),
            "base_ref": (pr.get("base") or {}).get("ref"),
        }

    def get_tree_paths(self, repo: str, ref: str, *, limit: int = 300) -> list[str]:
        """Árvore de arquivos do repo em `ref` (achado do disparo real: o Planner
        propõe expected_files ANTES do clone — sem a árvore ele adivinha caminhos
        e o plan_compliance reprova o diff real). Só paths de blob, truncado."""
        resp = httpx.get(
            f"{self._cfg.api_base_url}/repos/{repo}/git/trees/{ref}",
            headers=self._headers(),
            params={"recursive": "1"},
            timeout=20.0,
        )
        resp.raise_for_status()
        tree = resp.json().get("tree", [])
        return [t["path"] for t in tree if t.get("type") == "blob"][:limit]

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

    def rerequest_check_run(self, repo: str, check_run_id: int) -> bool:
        resp = httpx.post(
            f"{self._cfg.api_base_url}/repos/{repo}/check-runs/{check_run_id}/rerequest",
            headers=self._headers(),
            timeout=15.0,
        )
        if resp.status_code in (403, 404, 422):
            # CI do repo não suporta re-run por job (ou o run não é re-rodável).
            return False
        resp.raise_for_status()
        return True

    def list_review_threads(self, repo: str, pr_number: int) -> list[dict]:
        resp = httpx.get(
            f"{self._cfg.api_base_url}/repos/{repo}/pulls/{pr_number}/comments",
            headers=self._headers(),
            params={"per_page": 100},
            timeout=15.0,
        )
        resp.raise_for_status()
        return [
            {
                "id": c.get("id"),
                "commit_id": c.get("commit_id"),
                "original_commit_id": c.get("original_commit_id"),
                "path": c.get("path"),
            }
            for c in resp.json()
        ]

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

    # Fase 3 (WSE-E4-T9b) — targeted re-runs
    rerequest_supported: bool = True
    rerequested: list[tuple[str, int]] = field(default_factory=list)

    def rerequest_check_run(self, repo: str, check_run_id: int) -> bool:
        if not self.rerequest_supported:
            return False
        self.rerequested.append((repo, check_run_id))
        # imita o GitHub: o check-run re-rodado volta a "queued"
        for runs in self._check_runs.values():
            for run in runs:
                if run.get("id") == check_run_id:
                    run["status"] = "queued"
                    run["conclusion"] = None
        return True

    # Fase 4 (WSE-E6-T16) — threads de review ancoradas em commits.
    _review_threads: dict[tuple[str, int], list[dict]] = field(default_factory=dict)

    def set_review_threads(self, repo: str, pr_number: int, threads: list[dict]) -> None:
        """Só para teste — popula as threads de review "ancoradas em commits"."""
        self._review_threads[(repo, pr_number)] = threads

    def list_review_threads(self, repo: str, pr_number: int) -> list[dict]:
        return list(self._review_threads.get((repo, pr_number), []))

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
