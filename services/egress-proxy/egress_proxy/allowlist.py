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
    #: The inbound leg may be plain HTTP, but the OUTBOUND leg is re-originated
    #: over TLS on 443. This is what makes credential injection possible at all:
    #: injection needs the proxy to terminate the request, and terminating means
    #: plain HTTP from the sandbox — a hop that never leaves the Pod network.
    #: Nothing reaches the internet unencrypted, and `_handle_plain_http`
    #: refuses to inject onto any leg that is not TLS.
    tls_upgrade: bool = False


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

    def entry_for(self, host: str, port: int | None = None) -> AllowlistEntry | None:
        """The entry that governs this request.

        `port` is optional for the callers that only want the host's category,
        but anything reading a per-entry POLICY flag must pass it: a host can
        hold several entries with different rules — github.com has :443 for the
        CONNECT tunnel and :80 for the credential-injecting relay — and the
        host-only lookup returns whichever was declared first, which silently
        answered the wrong policy for the other port.

        An exact port match wins; a wildcard entry (`port=None`) is the
        fallback, never the other way round."""
        host = host.lower()
        wildcard: AllowlistEntry | None = None
        for e in self.entries:
            if e.host.lower() != host:
                continue
            if port is not None and e.port == port:
                return e
            if e.port is None and wildcard is None:
                wildcard = e
        return wildcard if port is not None else next(
            (e for e in self.entries if e.host.lower() == host), None
        )

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
            # The git smart-HTTP relay, and the ONLY reason a :80 entry exists.
            # A private repo cannot be cloned through a CONNECT tunnel: the proxy
            # would have to inject an Authorization header inside opaque TLS,
            # which it cannot do and should not try. So the sandbox speaks plain
            # HTTP to the proxy over the Pod network, the proxy injects the
            # installation token, and re-originates over TLS to :443.
            # `tls_upgrade` is what keeps the original comment above true — the
            # credential still never travels a cleartext hop to the internet.
            AllowlistEntry(
                host=repo_host,
                port=80,
                reason="git smart-HTTP relay — inbound plain HTTP, outbound re-originated over TLS",
                category="repo",
                tls_upgrade=True,
            ),
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
