"""Configuração via env var — nenhum segredo/credencial hardcoded.

Fase 1 / modo local: quando `GITHUB_APP_ID`/`GITHUB_APP_PRIVATE_KEY` não estão
setados, `dse_validation.github.client.build_github_client()` retorna um
`FakeGitHubClient` (fixture in-memory, ver github/client.py) em vez de falhar —
isso permite testar toda a lógica de PR finalizer/CI-status/comment-backend
sem uma GitHub App real registrada. Documentado explicitamente no README.
"""
from __future__ import annotations

import os
import shlex


def _env_cmd(name: str, default: str) -> list[str]:
    raw = os.environ.get(name, default)
    return shlex.split(raw)


class L1Config:
    """Comandos do pipeline L1 — configuráveis por env var porque o repo alvo
    (o que o Coder está editando) pode não ser este monorepo Python. Produção
    deve derivar isto do próprio repo (Makefile/package.json/pyproject) em vez
    de env vars fixas; documentado como pendência no README."""

    def __init__(self) -> None:
        self.lint_cmd = _env_cmd("DSE_L1_LINT_CMD", "ruff check .")
        self.typecheck_cmd = _env_cmd("DSE_L1_TYPECHECK_CMD", "mypy .")
        self.test_cmd = _env_cmd("DSE_L1_TEST_CMD", "pytest -q")
        self.build_cmd = _env_cmd("DSE_L1_BUILD_CMD", "python -m compileall -q .")
        self.timeout_seconds = int(os.environ.get("DSE_L1_TIMEOUT_SECONDS", "300"))
        self.sast_severity_gate = os.environ.get("DSE_L1_SAST_SEVERITY_GATE", "MEDIUM")


class GitHubConfig:
    def __init__(self) -> None:
        self.app_id = os.environ.get("GITHUB_APP_ID")
        self.private_key_pem = os.environ.get("GITHUB_APP_PRIVATE_KEY")
        self.installation_id = os.environ.get("GITHUB_APP_INSTALLATION_ID")
        self.api_base_url = os.environ.get("GITHUB_API_BASE_URL", "https://api.github.com")

    @property
    def is_configured(self) -> bool:
        return bool(self.app_id and self.private_key_pem and self.installation_id)


class StrictModeConfig:
    """WSE-E3-T8 (P1, opcional) — flag de "modo estrito": em vez de abrir PR,
    postar apenas um compare link. NÃO wired em `finalize_pr` nesta fase —
    ver README §Pendências para o motivo (conflita com o tipo de retorno
    `PrRef.pr_number: int` obrigatório do contrato)."""

    def __init__(self) -> None:
        self.enabled = os.environ.get("DSE_WSE_STRICT_MODE", "false").lower() in (
            "1",
            "true",
            "yes",
        )
