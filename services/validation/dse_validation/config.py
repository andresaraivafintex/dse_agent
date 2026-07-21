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


def _env_bool(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).lower() in ("1", "true", "yes")


class StrictModeConfig:
    """WSE-E3-T8 — flag de "modo estrito" por repo/tenant: em vez de abrir o PR,
    o finalizer só faz push do branch e retorna um `PrRef` com `compare_url`
    preenchido (`pr_number is None`) + posta o compare link no tracking comment;
    um humano abre o PR com 1 clique e o workflow adota o PR (mesmo WorkItem).

    Na Fase 2 o contrato `PrRef` ganhou `compare_url` e `pr_number` opcional, então
    isto agora ESTÁ wired em `finalize_pr_core` (ver `github/pr_finalizer.py`).

    Resolução da flag (mais específico ganha), tudo por env porque `tenant_config`
    (WS-F, tabela de fairness/budget/flags) ainda não expõe um campo de strict-mode:
      1. `DSE_WSE_STRICT_MODE_TENANT_<TENANT>_<REPO>` (repo com `/`->`_`, upper)
      2. `DSE_WSE_STRICT_MODE_TENANT_<TENANT>`
      3. `DSE_WSE_STRICT_MODE_REPOS` (lista separada por vírgula de `tenant:repo`)
      4. `DSE_WSE_STRICT_MODE` (global, default false)
    Quando WS-F publicar a flag por tenant em `tenant_config`, troca-se só
    `is_strict_for` para ler de lá — a assinatura não muda."""

    def __init__(self) -> None:
        self.global_enabled = _env_bool("DSE_WSE_STRICT_MODE")
        # Compat Fase 1: `.enabled` continua existindo (== flag global).
        self.enabled = self.global_enabled
        self._repo_allowlist = {
            entry.strip()
            for entry in os.environ.get("DSE_WSE_STRICT_MODE_REPOS", "").split(",")
            if entry.strip()
        }

    @staticmethod
    def _slug(value: str) -> str:
        return "".join(ch if ch.isalnum() else "_" for ch in value).upper()

    def is_strict_for(self, tenant_id: str, repo: str) -> bool:
        specific = os.environ.get(
            f"DSE_WSE_STRICT_MODE_TENANT_{self._slug(tenant_id)}_{self._slug(repo)}"
        )
        if specific is not None:
            return specific.lower() in ("1", "true", "yes")
        per_tenant = os.environ.get(f"DSE_WSE_STRICT_MODE_TENANT_{self._slug(tenant_id)}")
        if per_tenant is not None:
            return per_tenant.lower() in ("1", "true", "yes")
        if f"{tenant_id}:{repo}" in self._repo_allowlist:
            return True
        return self.global_enabled


class GarageConfig:
    """WSE-E5-T12 (Fase 3) — artifact store Garage (S3 self-hosted, sem SaaS).

    Endpoints default apontam para o serviço `garage` do docker-compose.wse.yml
    (portas reservadas 3900/3903 em CONVENTIONS.md). O token admin é DEV-ONLY —
    produção injeta via Vault/ESO (WS-F). A chave S3 usada pelo serviço é criada
    (idempotente) via admin API pelo próprio bootstrap — nenhum segredo S3 fica
    em env/arquivo."""

    def __init__(self) -> None:
        self.s3_endpoint = os.environ.get("DSE_GARAGE_S3_ENDPOINT", "http://localhost:3900")
        self.admin_endpoint = os.environ.get("DSE_GARAGE_ADMIN_ENDPOINT", "http://localhost:3903")
        self.admin_token = os.environ.get("DSE_GARAGE_ADMIN_TOKEN", "dse_garage_admin_dev")
        self.region = os.environ.get("DSE_GARAGE_REGION", "garage")
        self.key_name = os.environ.get("DSE_GARAGE_KEY_NAME", "dse-validation")
        # capacidade declarada do nó dev single-node (layout)
        self.layout_capacity = os.environ.get("DSE_GARAGE_LAYOUT_CAPACITY", "10G")
        # multipart a partir de 5 MiB (mínimo do protocolo S3; ADR-18 revisado)
        self.multipart_threshold_bytes = int(
            os.environ.get("DSE_GARAGE_MULTIPART_THRESHOLD", str(5 * 1024 * 1024))
        )
        self.bucket_prefix = os.environ.get("DSE_GARAGE_BUCKET_PREFIX", "dse-tenant-")


class PreviewConfig:
    """WSE-E4-T10 (Fase 3) — previews por PR via Argo CD no cluster k3d real.

    `repo_dir` é o repo git BARE de manifests servido ao cluster pelo container
    `dse-wse-gitserver` (nginx, dumb HTTP) — o host escreve por filesystem e o
    Argo CD lê por http://dse-wse-gitserver/<nome>.git (rede dse_net)."""

    def __init__(self) -> None:
        self.kube_context = os.environ.get("DSE_PREVIEW_KUBE_CONTEXT", "k3d-dse-preview")
        self.repo_dir = os.environ.get(
            "DSE_PREVIEW_REPO_DIR",
            os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "preview_repo"
            ),
        )
        # URL do repo VISTA DE DENTRO do cluster (rede dse_net)
        self.repo_url_in_cluster = os.environ.get(
            "DSE_PREVIEW_REPO_URL", "http://dse-wse-gitserver/preview-manifests.git"
        )
        self.argocd_namespace = os.environ.get("DSE_PREVIEW_ARGOCD_NS", "argocd")
        self.applicationset_name = os.environ.get("DSE_PREVIEW_APPSET_NAME", "dse-previews")
        self.default_ttl_seconds = int(os.environ.get("DSE_PREVIEW_TTL_SECONDS", "3600"))
        # ADR-26: cap de previews concorrentes por tenant desde o dia 1.
        # Override por tenant na tabela wse_preview_caps; este é o default.
        self.default_max_concurrent = int(os.environ.get("DSE_PREVIEW_MAX_CONCURRENT", "3"))
        # imagem do Deployment mínimo do preview (pinada, P7)
        self.preview_image = os.environ.get("DSE_PREVIEW_IMAGE", "nginx:1.27-alpine")
        self.sync_timeout_s = int(os.environ.get("DSE_PREVIEW_SYNC_TIMEOUT_S", "180"))


class L2Config:
    """WSE-E2 — parâmetros do loop L2 fresh-context + fix-retries bounded.

    - `max_fix_retries`: nº máximo de retornos L2->Coder antes de escalar a
      operador (P6 decline-never — nunca "insiste pra sempre").
    - `budget_cap_usd`: teto de custo acumulado (L2 + re-Coder) do loop; ao
      atingi-lo, escala em vez de gastar mais (P6). 0 = sem teto de custo
      (só o cap de iterações vale)."""

    def __init__(self) -> None:
        self.max_fix_retries = int(os.environ.get("DSE_L2_MAX_FIX_RETRIES", "3"))
        self.budget_cap_usd = float(os.environ.get("DSE_L2_BUDGET_CAP_USD", "0") or "0")
