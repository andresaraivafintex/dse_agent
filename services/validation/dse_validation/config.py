"""Configuração do serviço de validação.

Os comandos L1 são parte da política do repositório avaliado, não da
configuração do worker. Por isso, em execução real eles são carregados de
``.dse/validation.json`` no *base SHA* imutável. Variáveis ``DSE_L1_*_CMD``
não são mais uma fonte de verdade: além de permitirem deriva entre workers,
elas deixavam um comando vazio ser confundido com aprovação.

Timeouts e limiares operacionais ainda podem vir do ambiente. Eles não
escolhem que código executar e, portanto, não alteram a política confiável do
repositório.
"""
from __future__ import annotations

import json
import os
import re
from typing import TYPE_CHECKING, Any

from dse_contracts import GateStatus

if TYPE_CHECKING:
    from dse_validation.sandbox_exec import SandboxExecutor


L1_MANIFEST_PATH = ".dse/validation.json"
_FULL_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
_COMMAND_NAMES = ("lint", "typecheck", "test", "build")
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_COMMAND_ARGS = 128
_MAX_ARG_LENGTH = 4096


class L1ManifestError(ValueError):
    """Manifesto ausente ou inválido, com resultado de gate explícito."""

    def __init__(self, status: GateStatus, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _validate_command(name: str, raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise L1ManifestError(
            GateStatus.ERROR,
            f"commands.{name} deve ser um array JSON de argumentos, nunca uma string de shell",
        )
    if len(raw) > _MAX_COMMAND_ARGS:
        raise L1ManifestError(
            GateStatus.ERROR,
            f"commands.{name} excede o limite de {_MAX_COMMAND_ARGS} argumentos",
        )
    command: list[str] = []
    for index, arg in enumerate(raw):
        if not isinstance(arg, str) or not arg or "\x00" in arg:
            raise L1ManifestError(
                GateStatus.ERROR,
                f"commands.{name}[{index}] deve ser uma string não vazia e sem NUL",
            )
        if len(arg) > _MAX_ARG_LENGTH:
            raise L1ManifestError(
                GateStatus.ERROR,
                f"commands.{name}[{index}] excede {_MAX_ARG_LENGTH} caracteres",
            )
        command.append(arg)
    return command


class L1Config:
    """Política L1 materializada a partir de uma fonte rastreável.

    O construtor vazio é deliberadamente *fail-closed*: todos os comandos
    ficam não configurados. Testes unitários que precisam de comandos explícitos
    usam :meth:`for_test_repo`; o caminho de produção usa
    :meth:`from_trusted_manifest`.
    """

    def __init__(
        self,
        *,
        lint_cmd: list[str] | None = None,
        typecheck_cmd: list[str] | None = None,
        test_cmd: list[str] | None = None,
        build_cmd: list[str] | None = None,
        timeout_seconds: int | None = None,
        sast_severity_gate: str | None = None,
        source: str = "not-configured",
        manifest_status: GateStatus = GateStatus.NOT_CONFIGURED,
        manifest_detail: str = "manifesto L1 não carregado",
    ) -> None:
        self.lint_cmd = list(lint_cmd or [])
        self.typecheck_cmd = list(typecheck_cmd or [])
        self.test_cmd = list(test_cmd or [])
        self.build_cmd = list(build_cmd or [])
        self.timeout_seconds = timeout_seconds or int(
            os.environ.get("DSE_L1_TIMEOUT_SECONDS", "300")
        )
        self.sast_severity_gate = (
            sast_severity_gate or os.environ.get("DSE_L1_SAST_SEVERITY_GATE", "MEDIUM")
        ).upper()
        self.source = source
        self.manifest_status = manifest_status
        self.manifest_detail = manifest_detail

    @classmethod
    def for_test_repo(cls) -> "L1Config":
        """Config explícita para fixtures locais; nunca chamada pelo worker."""

        return cls(
            lint_cmd=["ruff", "check", "."],
            typecheck_cmd=["mypy", "."],
            test_cmd=["pytest", "-q"],
            build_cmd=["python", "-m", "compileall", "-q", "."],
            source="explicit-test-config",
            manifest_status=GateStatus.PASS,
            manifest_detail="configuração explícita de teste",
        )

    @classmethod
    def from_trusted_manifest(
        cls,
        executor: "SandboxExecutor",
        base_sha: str,
        *,
        manifest_path: str = L1_MANIFEST_PATH,
    ) -> "L1Config":
        """Carrega a política do commit-base, nunca do checkout mutável.

        A API recebe argumentos como array e os repassa sem shell. O caminho
        do manifesto é constante no chamador de produção; o parâmetro existe
        apenas para testes de contrato.
        """

        source = f"{base_sha}:{manifest_path}"
        try:
            if not _FULL_GIT_SHA_RE.fullmatch(base_sha):
                raise L1ManifestError(
                    GateStatus.ERROR,
                    "base_sha deve ser um SHA Git completo de 40 ou 64 caracteres hexadecimais",
                )

            verify = executor.run(
                ["git", "cat-file", "-e", f"{base_sha}^{{commit}}"], timeout=15
            )
            if not verify.ok:
                raise L1ManifestError(
                    GateStatus.ERROR,
                    f"base_sha {base_sha} não existe como commit no sandbox",
                )

            rendered = executor.run(
                ["git", "show", f"{base_sha}:{manifest_path}"], timeout=15
            )
            if not rendered.ok:
                raise L1ManifestError(
                    GateStatus.NOT_CONFIGURED,
                    f"manifesto confiável ausente em {source}",
                )
            if len(rendered.stdout.encode("utf-8")) > _MAX_MANIFEST_BYTES:
                raise L1ManifestError(
                    GateStatus.ERROR,
                    f"manifesto {source} excede {_MAX_MANIFEST_BYTES} bytes",
                )
            try:
                payload = json.loads(rendered.stdout)
            except json.JSONDecodeError as exc:
                raise L1ManifestError(
                    GateStatus.ERROR,
                    f"manifesto {source} contém JSON inválido: {exc.msg}",
                ) from exc
            return cls._from_manifest_payload(payload, source=source)
        except L1ManifestError as exc:
            return cls(
                source=source,
                manifest_status=exc.status,
                manifest_detail=exc.detail,
            )

    @classmethod
    def _from_manifest_payload(cls, payload: Any, *, source: str) -> "L1Config":
        if not isinstance(payload, dict):
            raise L1ManifestError(GateStatus.ERROR, f"manifesto {source} deve ser um objeto JSON")
        allowed = {"version", "commands", "timeout_seconds", "sast_severity_gate"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise L1ManifestError(
                GateStatus.ERROR,
                f"manifesto {source} possui campos desconhecidos: {unknown}",
            )
        if payload.get("version") != 1:
            raise L1ManifestError(
                GateStatus.ERROR,
                f"manifesto {source} exige version=1",
            )
        commands = payload.get("commands")
        if not isinstance(commands, dict):
            raise L1ManifestError(
                GateStatus.ERROR,
                f"manifesto {source} exige objeto commands",
            )
        unknown_commands = sorted(set(commands) - set(_COMMAND_NAMES))
        if unknown_commands:
            raise L1ManifestError(
                GateStatus.ERROR,
                f"manifesto {source} possui comandos desconhecidos: {unknown_commands}",
            )

        timeout = payload.get(
            "timeout_seconds", int(os.environ.get("DSE_L1_TIMEOUT_SECONDS", "300"))
        )
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 3600:
            raise L1ManifestError(
                GateStatus.ERROR,
                f"manifesto {source}: timeout_seconds deve estar entre 1 e 3600",
            )
        severity = payload.get(
            "sast_severity_gate", os.environ.get("DSE_L1_SAST_SEVERITY_GATE", "MEDIUM")
        )
        if not isinstance(severity, str) or severity.upper() not in {"LOW", "MEDIUM", "HIGH"}:
            raise L1ManifestError(
                GateStatus.ERROR,
                f"manifesto {source}: sast_severity_gate deve ser LOW, MEDIUM ou HIGH",
            )

        parsed = {name: _validate_command(name, commands.get(name)) for name in _COMMAND_NAMES}
        return cls(
            lint_cmd=parsed["lint"],
            typecheck_cmd=parsed["typecheck"],
            test_cmd=parsed["test"],
            build_cmd=parsed["build"],
            timeout_seconds=timeout,
            sast_severity_gate=severity,
            source=source,
            manifest_status=GateStatus.PASS,
            manifest_detail=f"manifesto confiável carregado de {source}",
        )


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
        # imagem do Deployment do preview (pinada, P7). Plano 08 §D (D4): por
        # padrão sobe um placeholder nginx (prova o fluxo); em piloto, aponte
        # para a imagem REAL do PR (buildada/pushada no CI) via este env — o
        # build-por-PR + registry acompanham o cluster (decisão de infra).
        self.preview_image = os.environ.get("DSE_PREVIEW_IMAGE", "nginx:1.27-alpine")
        # plano 08 §D (D3): host EXTERNO acessível pelo browser. Sem isto a URL
        # é o DNS interno do cluster (não clicável de fora). Ex.:
        #   dev local : "http://{namespace}.preview.localhost:8081" (Traefik do
        #               k3d publicado em localhost:8081; *.localhost resolve
        #               p/ 127.0.0.1 nos browsers modernos)
        #   túnel     : "https://{namespace}.preview.SEUDOMINIO.com" (cloudflared
        #               apontando para localhost:8081 — mesmo Ingress)
        #   VPS depois: mesmo template, só muda o DNS. Ver infra/preview-exposure.md.
        # `{namespace}` é substituído. Quando setado, build_manifests também
        # gera o INGRESS com esse hostname (senão nenhum Ingress é criado).
        self.external_host_template = os.environ.get("DSE_PREVIEW_EXTERNAL_HOST", "")
        self.ingress_class = os.environ.get("DSE_PREVIEW_INGRESS_CLASS", "traefik")
        # plano 08 §D (D4): porta em que o APP do PR escuta dentro do container
        # (o Service/Ingress publicam sempre 80 → targetPort=app_port).
        self.app_port = int(os.environ.get("DSE_PREVIEW_APP_PORT", "80"))
        # D4 — build da imagem REAL do PR: quando true e o workspace da tarefa
        # tem Dockerfile, o preview builda/pusha a imagem do head do PR em vez
        # do placeholder. push_ref = visto pelo daemon local (localhost:5510);
        # pull_ref = visto pelos nodes do cluster (k3d-dse-registry:5510).
        self.build_image = _env_bool("DSE_PREVIEW_BUILD_IMAGE", "false")
        self.registry_push = os.environ.get("DSE_PREVIEW_REGISTRY_PUSH", "localhost:5510")
        self.registry_pull = os.environ.get("DSE_PREVIEW_REGISTRY_PULL", "k3d-dse-registry:5510")
        self.build_timeout_s = int(os.environ.get("DSE_PREVIEW_BUILD_TIMEOUT_S", "420"))
        self.sync_timeout_s = int(os.environ.get("DSE_PREVIEW_SYNC_TIMEOUT_S", "180"))

    def preview_url_for(self, namespace: str) -> str:
        """URL do preview. Externa (browser-reachable) quando
        `external_host_template` está setado; senão o DNS interno do cluster
        (útil só de dentro — o link ainda aparece no PR, D1, mas D3 o torna
        clicável)."""
        if self.external_host_template:
            return self.external_host_template.replace("{namespace}", namespace)
        return f"http://preview.{namespace}.svc.cluster.local"

    def external_hostname_for(self, namespace: str) -> str | None:
        """Hostname puro (sem scheme/porta) para o campo `host` do Ingress —
        derivado do mesmo template da URL (D3). None quando não configurado."""
        if not self.external_host_template:
            return None
        url = self.external_host_template.replace("{namespace}", namespace)
        host = url.split("://", 1)[-1].split("/", 1)[0]
        return host.split(":", 1)[0] or None


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
