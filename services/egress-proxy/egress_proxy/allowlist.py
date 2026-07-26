"""Allowlist derived from the WorkItem (WSC-E2-T1/T3). Default-deny: any host
not explicitly listed here is refused."""
from __future__ import annotations

from dataclasses import dataclass, field

# Package registries allowed by default (Phase 1: hardcoded; Phase 2/WSF could
# make this configurable per tenant via tenant_config).
DEFAULT_PACKAGE_REGISTRIES: tuple[str, ...] = (
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
)


@dataclass(frozen=True)
class AllowlistEntry:
    host: str
    port: int | None = None  # None == any port
    reason: str = ""
    category: str = "generic"  # "repo" | "model_gateway" | "package_registry" | "generic"


@dataclass
class Allowlist:
    entries: list[AllowlistEntry] = field(default_factory=list)

    def is_allowed(self, host: str, port: int) -> bool:
        host = host.lower()
        for e in self.entries:
            if e.host.lower() != host:
                continue
            if e.port is None or e.port == port:
                return True
        return False

    def entry_for(self, host: str) -> AllowlistEntry | None:
        host = host.lower()
        for e in self.entries:
            if e.host.lower() == host:
                return e
        return None

    @classmethod
    def for_work_item(
        cls,
        *,
        repo_host: str = "github.com",
        repo_api_host: str = "api.github.com",
        model_gateway_host: str = "localhost",
        model_gateway_port: int = 4000,
        package_registries: tuple[str, ...] | None = None,
    ) -> "Allowlist":
        """The ONLY allowlist entry for model calls is the model-gateway
        (WS-D)/LiteLLM (WSC-E2-T3) — no external provider (api.anthropic.com,
        api.openai.com, bedrock-runtime.*) is ever added here."""
        entries = [
            # port=443 explicit (not None): closes the plain-HTTP :80 vector, where
            # the injected credential could go out in the clear. Git/HTTPS use 443.
            AllowlistEntry(host=repo_host, port=443, reason="git remote of the task repo", category="repo"),
            AllowlistEntry(host=repo_api_host, port=443, reason="GitHub REST API (push/status)", category="repo"),
            AllowlistEntry(
                host=model_gateway_host,
                port=model_gateway_port,
                reason="model-gateway (WS-D)/LiteLLM — the only allowlist entry for model calls",
                category="model_gateway",
            ),
        ]
        for reg in package_registries or DEFAULT_PACKAGE_REGISTRIES:
            entries.append(AllowlistEntry(host=reg, reason="allowed package registry", category="package_registry"))
        return cls(entries=entries)
