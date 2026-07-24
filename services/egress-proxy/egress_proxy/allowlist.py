"""Allowlist derivada do WorkItem (WSC-E2-T1/T3). Default-deny: qualquer
host que não esteja explicitamente aqui é recusado."""
from __future__ import annotations

from dataclasses import dataclass, field

# Registries de pacote permitidos por padrão (Fase 1: hardcoded; Fase 2/WSF
# poderia tornar isso configurável por tenant via tenant_config).
DEFAULT_PACKAGE_REGISTRIES: tuple[str, ...] = (
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
)


@dataclass(frozen=True)
class AllowlistEntry:
    host: str
    port: int | None = None  # None == qualquer porta
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
        """A ÚNICA allowlist entry para chamadas de modelo é o model-gateway
        (WS-D)/LiteLLM (WSC-E2-T3) — nenhum provider externo (api.anthropic.com,
        api.openai.com, bedrock-runtime.*) é adicionado aqui, nunca."""
        entries = [
            # port=443 explícito (não None): fecha o vetor HTTP-plano :80, onde a
            # injeção de credencial poderia sair em claro. Git/HTTPS usam 443.
            AllowlistEntry(host=repo_host, port=443, reason="repo git remote da tarefa", category="repo"),
            AllowlistEntry(host=repo_api_host, port=443, reason="REST API do GitHub (push/status)", category="repo"),
            AllowlistEntry(
                host=model_gateway_host,
                port=model_gateway_port,
                reason="model-gateway (WS-D)/LiteLLM — única allowlist entry para chamadas de modelo",
                category="model_gateway",
            ),
        ]
        for reg in package_registries or DEFAULT_PACKAGE_REGISTRIES:
            entries.append(AllowlistEntry(host=reg, reason="registry de pacotes permitido", category="package_registry"))
        return cls(entries=entries)
