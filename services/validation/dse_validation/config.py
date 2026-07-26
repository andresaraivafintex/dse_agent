"""Validation service configuration.

The L1 commands are part of the policy of the repository under evaluation, not
of the worker's configuration. That is why, in a real run, they are loaded from
``.dse/validation.json`` at the immutable *base SHA*. ``DSE_L1_*_CMD`` variables
are no longer a source of truth: besides allowing drift between workers, they
let an empty command be mistaken for approval.

Operational timeouts and thresholds may still come from the environment. They
do not choose which code to run and therefore do not change the repository's
trusted policy.
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
    """Missing or invalid manifest, carrying an explicit gate outcome."""

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
            f"commands.{name} must be a JSON array of arguments, never a shell string",
        )
    if len(raw) > _MAX_COMMAND_ARGS:
        raise L1ManifestError(
            GateStatus.ERROR,
            f"commands.{name} exceeds the limit of {_MAX_COMMAND_ARGS} arguments",
        )
    command: list[str] = []
    for index, arg in enumerate(raw):
        if not isinstance(arg, str) or not arg or "\x00" in arg:
            raise L1ManifestError(
                GateStatus.ERROR,
                f"commands.{name}[{index}] must be a non-empty string with no NUL byte",
            )
        if len(arg) > _MAX_ARG_LENGTH:
            raise L1ManifestError(
                GateStatus.ERROR,
                f"commands.{name}[{index}] exceeds {_MAX_ARG_LENGTH} characters",
            )
        command.append(arg)
    return command


class L1Config:
    """L1 policy materialized from a traceable source.

    The empty constructor is deliberately *fail-closed*: every command ends up
    not configured. Unit tests that need explicit commands use
    :meth:`for_test_repo`; the production path uses
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
        manifest_detail: str = "L1 manifest not loaded",
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
        """Explicit config for local fixtures; never called by the worker."""

        return cls(
            lint_cmd=["ruff", "check", "."],
            typecheck_cmd=["mypy", "."],
            test_cmd=["pytest", "-q"],
            build_cmd=["python", "-m", "compileall", "-q", "."],
            source="explicit-test-config",
            manifest_status=GateStatus.PASS,
            manifest_detail="explicit test configuration",
        )

    @classmethod
    def from_trusted_manifest(
        cls,
        executor: "SandboxExecutor",
        base_sha: str,
        *,
        manifest_path: str = L1_MANIFEST_PATH,
    ) -> "L1Config":
        """Loads the policy from the base commit, never from the mutable checkout.

        The API takes arguments as an array and passes them through without a
        shell. The manifest path is constant in the production caller; the
        parameter exists only for contract tests.
        """

        source = f"{base_sha}:{manifest_path}"
        try:
            if not _FULL_GIT_SHA_RE.fullmatch(base_sha):
                raise L1ManifestError(
                    GateStatus.ERROR,
                    "base_sha must be a full Git SHA of 40 or 64 hexadecimal characters",
                )

            verify = executor.run(
                ["git", "cat-file", "-e", f"{base_sha}^{{commit}}"], timeout=15
            )
            if not verify.ok:
                raise L1ManifestError(
                    GateStatus.ERROR,
                    f"base_sha {base_sha} does not exist as a commit in the sandbox",
                )

            rendered = executor.run(
                ["git", "show", f"{base_sha}:{manifest_path}"], timeout=15
            )
            if not rendered.ok:
                raise L1ManifestError(
                    GateStatus.NOT_CONFIGURED,
                    f"trusted manifest missing at {source}",
                )
            if len(rendered.stdout.encode("utf-8")) > _MAX_MANIFEST_BYTES:
                raise L1ManifestError(
                    GateStatus.ERROR,
                    f"manifest {source} exceeds {_MAX_MANIFEST_BYTES} bytes",
                )
            try:
                payload = json.loads(rendered.stdout)
            except json.JSONDecodeError as exc:
                raise L1ManifestError(
                    GateStatus.ERROR,
                    f"manifest {source} contains invalid JSON: {exc.msg}",
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
            raise L1ManifestError(GateStatus.ERROR, f"manifest {source} must be a JSON object")
        allowed = {"version", "commands", "timeout_seconds", "sast_severity_gate"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise L1ManifestError(
                GateStatus.ERROR,
                f"manifest {source} has unknown fields: {unknown}",
            )
        if payload.get("version") != 1:
            raise L1ManifestError(
                GateStatus.ERROR,
                f"manifest {source} requires version=1",
            )
        commands = payload.get("commands")
        if not isinstance(commands, dict):
            raise L1ManifestError(
                GateStatus.ERROR,
                f"manifest {source} requires a commands object",
            )
        unknown_commands = sorted(set(commands) - set(_COMMAND_NAMES))
        if unknown_commands:
            raise L1ManifestError(
                GateStatus.ERROR,
                f"manifest {source} has unknown commands: {unknown_commands}",
            )

        timeout = payload.get(
            "timeout_seconds", int(os.environ.get("DSE_L1_TIMEOUT_SECONDS", "300"))
        )
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 3600:
            raise L1ManifestError(
                GateStatus.ERROR,
                f"manifest {source}: timeout_seconds must be between 1 and 3600",
            )
        severity = payload.get(
            "sast_severity_gate", os.environ.get("DSE_L1_SAST_SEVERITY_GATE", "MEDIUM")
        )
        if not isinstance(severity, str) or severity.upper() not in {"LOW", "MEDIUM", "HIGH"}:
            raise L1ManifestError(
                GateStatus.ERROR,
                f"manifest {source}: sast_severity_gate must be LOW, MEDIUM or HIGH",
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
            manifest_detail=f"trusted manifest loaded from {source}",
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
    """WSE-E3-T8 — per repo/tenant "strict mode" flag: instead of opening the PR,
    the finalizer only pushes the branch and returns a `PrRef` with `compare_url`
    filled in (`pr_number is None`) + posts the compare link on the tracking
    comment; a human opens the PR with 1 click and the workflow adopts that PR
    (same WorkItem).

    In Phase 2 the `PrRef` contract gained `compare_url` and an optional
    `pr_number`, so this IS now wired into `finalize_pr_core` (see
    `github/pr_finalizer.py`).

    Flag resolution (most specific wins), all via env because `tenant_config`
    (WS-F, the fairness/budget/flags table) does not expose a strict-mode field yet:
      1. `DSE_WSE_STRICT_MODE_TENANT_<TENANT>_<REPO>` (repo with `/`->`_`, upper)
      2. `DSE_WSE_STRICT_MODE_TENANT_<TENANT>`
      3. `DSE_WSE_STRICT_MODE_REPOS` (comma-separated list of `tenant:repo`)
      4. `DSE_WSE_STRICT_MODE` (global, default false)
    Once WS-F publishes the per-tenant flag in `tenant_config`, only
    `is_strict_for` changes to read from there — the signature stays the same."""

    def __init__(self) -> None:
        self.global_enabled = _env_bool("DSE_WSE_STRICT_MODE")
        # Phase 1 compat: `.enabled` still exists (== global flag).
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
    """WSE-E5-T12 (Phase 3) — Garage artifact store (self-hosted S3, no SaaS).

    Default endpoints point at the `garage` service in docker-compose.wse.yml
    (ports 3900/3903 reserved in CONVENTIONS.md). The admin token is DEV-ONLY —
    production injects it via Vault/ESO (WS-F). The S3 key used by the service is
    created (idempotently) through the admin API by the bootstrap itself — no S3
    secret lives in env/files."""

    def __init__(self) -> None:
        self.s3_endpoint = os.environ.get("DSE_GARAGE_S3_ENDPOINT", "http://localhost:3900")
        self.admin_endpoint = os.environ.get("DSE_GARAGE_ADMIN_ENDPOINT", "http://localhost:3903")
        self.admin_token = os.environ.get("DSE_GARAGE_ADMIN_TOKEN", "dse_garage_admin_dev")
        self.region = os.environ.get("DSE_GARAGE_REGION", "garage")
        self.key_name = os.environ.get("DSE_GARAGE_KEY_NAME", "dse-validation")
        # declared capacity of the single-node dev node (layout)
        self.layout_capacity = os.environ.get("DSE_GARAGE_LAYOUT_CAPACITY", "10G")
        # multipart from 5 MiB up (S3 protocol minimum; revised ADR-18)
        self.multipart_threshold_bytes = int(
            os.environ.get("DSE_GARAGE_MULTIPART_THRESHOLD", str(5 * 1024 * 1024))
        )
        self.bucket_prefix = os.environ.get("DSE_GARAGE_BUCKET_PREFIX", "dse-tenant-")


class PreviewConfig:
    """WSE-E4-T10 (Phase 3) — per-PR previews via Argo CD on the real k3d cluster.

    `repo_dir` is the BARE git repo of manifests served to the cluster by the
    `dse-wse-gitserver` container (nginx, dumb HTTP) — the host writes through
    the filesystem and Argo CD reads via http://dse-wse-gitserver/<name>.git
    (dse_net network)."""

    def __init__(self) -> None:
        self.kube_context = os.environ.get("DSE_PREVIEW_KUBE_CONTEXT", "k3d-dse-preview")
        self.repo_dir = os.environ.get(
            "DSE_PREVIEW_REPO_DIR",
            os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "preview_repo"
            ),
        )
        # repo URL AS SEEN FROM INSIDE the cluster (dse_net network)
        self.repo_url_in_cluster = os.environ.get(
            "DSE_PREVIEW_REPO_URL", "http://dse-wse-gitserver/preview-manifests.git"
        )
        self.argocd_namespace = os.environ.get("DSE_PREVIEW_ARGOCD_NS", "argocd")
        self.applicationset_name = os.environ.get("DSE_PREVIEW_APPSET_NAME", "dse-previews")
        self.default_ttl_seconds = int(os.environ.get("DSE_PREVIEW_TTL_SECONDS", "3600"))
        # ADR-26: cap on concurrent previews per tenant from day 1.
        # Per-tenant override lives in the wse_preview_caps table; this is the default.
        self.default_max_concurrent = int(os.environ.get("DSE_PREVIEW_MAX_CONCURRENT", "3"))
        # image of the preview Deployment (pinned, P7). Plan 08 §D (D4): by
        # default it brings up an nginx placeholder (proves the flow); in a
        # pilot, point this env at the REAL image of the PR (built/pushed in
        # CI) — per-PR build + registry come along with the cluster (infra
        # decision).
        self.preview_image = os.environ.get("DSE_PREVIEW_IMAGE", "nginx:1.27-alpine")
        # plan 08 §D (D3): EXTERNAL host reachable from the browser. Without it
        # the URL is the cluster-internal DNS (not clickable from outside). E.g.:
        #   local dev : "http://{namespace}.preview.localhost:8081" (k3d's
        #               Traefik published on localhost:8081; *.localhost
        #               resolves to 127.0.0.1 in modern browsers)
        #   tunnel    : "https://{namespace}.preview.YOURDOMAIN.com" (cloudflared
        #               pointing at localhost:8081 — same Ingress)
        #   VPS later : same template, only the DNS changes. See
        #               infra/preview-exposure.md.
        # `{namespace}` is substituted. When set, build_manifests also generates
        # the INGRESS with that hostname (otherwise no Ingress is created).
        self.external_host_template = os.environ.get("DSE_PREVIEW_EXTERNAL_HOST", "")
        self.ingress_class = os.environ.get("DSE_PREVIEW_INGRESS_CLASS", "traefik")
        # plan 08 §D (D4): port the PR's APP listens on inside the container
        # (Service/Ingress always publish 80 → targetPort=app_port).
        self.app_port = int(os.environ.get("DSE_PREVIEW_APP_PORT", "80"))
        # How the preview serves the PR.
        #   "image"  — deploy a prebuilt image (the original design: needs a
        #              Docker daemon to build it and a registry to pull it from).
        #   "source" — run the branch straight from source in the container
        #              (clone + install + start). This is the only mode that
        #              works on a cluster with no Docker and no registry, which
        #              is exactly what the gVisor/k8s sandbox substrate is.
        self.mode = os.environ.get("DSE_PREVIEW_MODE", "image")
        # How the manifests reach the cluster.
        #   "gitops" — write to the manifests repo and let Argo CD sync it.
        #   "kubectl" — apply directly. No Argo CD to install and no GitOps repo
        #              to host; the tradeoff is losing Argo's own GC, so the TTL
        #              reaper becomes the only thing that cleans previews up.
        self.apply_mode = os.environ.get("DSE_PREVIEW_APPLY", "gitops")
        # Base image for `mode="source"`: it only needs a runtime plus git, so
        # the language runtime image is enough. Pinned (P7).
        self.source_image = os.environ.get("DSE_PREVIEW_SOURCE_IMAGE", "node:22-alpine")
        # Port the app listens on when run from source. Node/Express honour
        # $PORT, so the container sets it and the Service targets the same one.
        self.source_port = int(os.environ.get("DSE_PREVIEW_SOURCE_PORT", "3000"))
        # Cloning and installing dependencies takes far longer than starting a
        # prebuilt image, so readiness has to be patient or the Deployment is
        # declared failed while npm is still resolving.
        self.source_ready_timeout_s = int(os.environ.get("DSE_PREVIEW_SOURCE_READY_TIMEOUT", "300"))
        # D4 — build of the REAL PR image: when true and the task workspace has
        # a Dockerfile, the preview builds/pushes the image of the PR head
        # instead of the placeholder. push_ref = as seen by the local daemon
        # (localhost:5510); pull_ref = as seen by the cluster nodes
        # (k3d-dse-registry:5510).
        self.build_image = _env_bool("DSE_PREVIEW_BUILD_IMAGE", "false")
        self.registry_push = os.environ.get("DSE_PREVIEW_REGISTRY_PUSH", "localhost:5510")
        self.registry_pull = os.environ.get("DSE_PREVIEW_REGISTRY_PULL", "k3d-dse-registry:5510")
        self.build_timeout_s = int(os.environ.get("DSE_PREVIEW_BUILD_TIMEOUT_S", "420"))
        self.sync_timeout_s = int(os.environ.get("DSE_PREVIEW_SYNC_TIMEOUT_S", "180"))

    def preview_url_for(self, namespace: str) -> str:
        """Preview URL. External (browser-reachable) when
        `external_host_template` is set; otherwise the cluster-internal DNS
        (useful only from the inside — the link still shows up on the PR, D1,
        but D3 is what makes it clickable)."""
        if self.external_host_template:
            return self.external_host_template.replace("{namespace}", namespace)
        return f"http://preview.{namespace}.svc.cluster.local"

    def external_hostname_for(self, namespace: str) -> str | None:
        """Bare hostname (no scheme/port) for the Ingress `host` field —
        derived from the same URL template (D3). None when not configured."""
        if not self.external_host_template:
            return None
        url = self.external_host_template.replace("{namespace}", namespace)
        host = url.split("://", 1)[-1].split("/", 1)[0]
        return host.split(":", 1)[0] or None


class L2Config:
    """WSE-E2 — parameters of the L2 fresh-context loop + bounded fix-retries.

    - `max_fix_retries`: max number of L2->Coder round-trips before escalating
      to an operator (P6 decline-never — never "keeps trying forever").
    - `budget_cap_usd`: cap on the loop's accumulated cost (L2 + re-Coder); on
      reaching it, escalate instead of spending more (P6). 0 = no cost cap
      (only the iteration cap applies)."""

    def __init__(self) -> None:
        self.max_fix_retries = int(os.environ.get("DSE_L2_MAX_FIX_RETRIES", "3"))
        self.budget_cap_usd = float(os.environ.get("DSE_L2_BUDGET_CAP_USD", "0") or "0")
