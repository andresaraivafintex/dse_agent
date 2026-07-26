# OSS Bill of Materials — Fintex DSE (Phase 1)

Owner: WS-F. Lists the open-source infrastructure components used in
steady state (container images + the larger Python libraries),
their licenses, and the implication for a regulated fintech client (self-hosted,
with no obligation to share the client's proprietary code).

This document covers **infrastructure and runtime**, not every
`pip` transitive dependency (those are audited via `pip-licenses`/a supply-chain
SBOM — out of scope for Phase 1, see the note at the end).

## Infrastructure components (container images)

| Component | Image | License | Use in the DSE | Obligations |
|---|---|---|---|---|
| PostgreSQL | `postgres:16-alpine` | PostgreSQL License (permissive, MIT/BSD-style) | Control plane (work_items, audit_log, tenant_config, identity map) | None — attribution optional, no copyleft |
| Temporal | `temporalio/auto-setup`, `temporalio/ui` | MIT | Durable orchestration of the WorkItem lifecycle | None — permissive |
| Redis | `redis:7-alpine` | RSALv2 / SSPLv1 (dual license, Redis 7.x) — **caution**: as of Redis 7.4/8, Redis Inc. changed the license from BSD to RSAL/SSPL | Cache/lightweight queue (internal use, not as a hosted multi-tenant service) | Internal self-hosted use (not offered as a service to third parties) typically falls outside the SSPL trigger, but **WS-F recommendation**: evaluate migrating to `valkey` (a BSD-3-Clause fork maintained by the Linux Foundation) before the first production client, or confirm with legal that internal use does not trigger SSPL/RSAL |
| HashiCorp Vault | `hashicorp/vault:1.17` | BUSL 1.1 (Business Source License) since Vault 1.11+ — **not pure OSS** | Secrets backend (dev mode in this Phase 1) | BUSL allows non-competitive production use (not offering Vault as a service competing with HashiCorp) — the DSE's internal use is covered; document for the client that a HashiCorp commercial license may be required depending on the desired volume/support. A 100% OSS alternative (MPL 2.0): OpenBao (a Linux Foundation post-BUSL fork) — evaluate before the pilot if BUSL is a contractual blocker |
| OpenTelemetry Collector | `otel/opentelemetry-collector-contrib` | Apache 2.0 | Span/metric collection (WSF-E7-T1) | None — permissive |

## Main Python libraries (runtime, not dev-only)

| Library | License | Use |
|---|---|---|
| LiteLLM | MIT | Model gateway (WS-D) — unified proxy for LLM providers |
| FastAPI | MIT | Every HTTP service (adapters, ingest-gateway, validation, platform) |
| Pydantic v2 | MIT | Data validation (`dse_contracts` contracts) |
| Temporal Python SDK | MIT | Workers/workflows (`services/orchestrator`) |
| psycopg2-binary | LGPL 3.0 (with an exception — permits dynamic linking without application copyleft) | Postgres driver |
| hvac | Apache 2.0 | Python client for Vault (`services/platform/dse_secrets`) |
| requests | Apache 2.0 | HTTP client (`dse_secrets` fallback without hvac) |

## Recommended action before the first production client

1. **Vault (BUSL)**: an explicit legal decision — accept BUSL for internal
   self-hosted use, or migrate to OpenBao (MPL 2.0). Blocks
   `WSF-E5-T3` (topology B) if the client contractually requires pure OSS.
2. **Redis (RSAL/SSPL)**: same decision — migrating to Valkey is a low-effort
   image swap (protocol compatible) if needed.
3. **Full SBOM of transitive dependencies**: run `pip-licenses` (or `cyclonedx-py`)
   over every `pyproject.toml` in the monorepo and attach it to this document — not done
   in this session because each workstream is still pinning its own
   dependencies in parallel (it only makes sense at the consolidation phase, with
   the final dependency tree frozen).
