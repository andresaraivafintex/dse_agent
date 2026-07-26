# Contracts changelog — Fintex DSE

Version history for the packages published under `packages/` (stable
cross-workstream contracts, per `CONVENTIONS.md`). Maintained by WS-F
(WSF-E0) as part of the platform CI/CD foundation.

## Contract change rule

> **Contract changes require chief-architect approval.**

Concretely:

1. **Additive changes are always allowed without prior approval** (adding a
   new optional field, a new function, a new constant) — as long as nothing
   that already exists is removed, renamed, or has its type/signature changed
   (`CONVENTIONS.md`: "add a new field/type without removing or renaming what
   already exists"). This covers the `dse_audit` extension made by WS-F in
   this Phase 1 (`dse_audit.queries` — see the entry below).
2. **Any breaking change** (removing/renaming a public field, changing the
   signature of a public function, changing the semantics of a status/enum
   already consumed by another workstream) requires:
   - an isolated PR containing only the contract change (never mixed with
     business logic for a specific service);
   - explicit approval from the program's chief architect (not just from the
     lead of the workstream that needs the change) — P3 (no agent session
     approves its own work) applies here too: whoever proposes the contract
     change cannot be the one who approves it;
   - a MAJOR version bump (see semver below) and a new entry in this
     changelog **before** the merge, not after;
   - notification in the channels of the consuming workstreams (see "consumed
     by" in each entry) — the merge must not surprise anyone who depends on
     the contract.
3. No workstream should reimplement an already-published contract (e.g. a
   local copy of `ConversationEvent`, or a second write path into the audit
   ledger outside `dse_audit.emit`) — that breaks the "single source of
   truth" guarantee the contract exists to provide.

Versioning: semver (`MAJOR.MINOR.PATCH`) per package, declared in each one's
`pyproject.toml`. MAJOR = breaking; MINOR = additive; PATCH = fix with no
change to the public surface.

## Packages and current versions

| Package | Version | Owner | Consumed by |
|---|---|---|---|
| `dse_contracts` (`packages/contracts`) | 0.1.0 | Foundation | WS-A, WS-B, WS-C, WS-D, WS-E, WS-F |
| `dse_audit` (`packages/dse_audit`) | 0.1.0 | Foundation (minimal) → **extended by WS-F in Phase 1** | Everyone (via `emit`); `dse_audit.queries` (reconstruction/export) consumed by any compliance service/report |
| `dse_identity` (`packages/dse_identity`) | 0.1.0 | Foundation (minimal) | WS-A (adapters resolve `platform_user_id` before writing `actor`) |

## Entries

### `dse_audit` 0.1.0 → additive extension (WSF-E1-T2, no version bump declared in pyproject — see note below)

- **What:** new module `dse_audit/queries.py` with
  `reconstruct_work_item_history(work_item_id) -> list[dict]`,
  `export_audit_range(tenant_id, start, end) -> list[dict]` and
  `export_audit_range_csv(...) -> str`. Re-exported from `dse_audit/__init__.py`
  alongside the pre-existing symbols (`emit`, `get_connection` — neither of
  which was removed/renamed).
- **Why:** Phase 1 exit criterion ("first audit-based reconstruction
  exercise passes") + compliance-grade export per tenant/period.
- **Change type:** additive (rule 1 above) — does not require prior
  chief-architect approval, but **is documented here for cross-workstream
  visibility**, since `packages/dse_audit` is a foundation directory and
  other workstreams may (reasonably) not expect changes in it.
- **Process note:** `dse_audit`'s `pyproject.toml` still declares
  `version = "0.1.0"` — WS-F's recommendation for the final consolidation:
  bump to `0.2.0` (MINOR, additive) in the integration PR, since a new
  version was in fact published.
- **Consumed by:** any service/report that needs to answer "what happened to
  WorkItem X" or produce an audit export — no real consumer integrated yet in
  this session (cross-workstream, integration happens in the consolidation
  phase).

### `services/platform` (dse-platform) 0.1.0 → new package (WSF-E2-T3a)

- **What:** `dse_secrets` — Vault client (`SecretsClient`, `get_secret`,
  `put_secret`, `delete_secret`). It is not a package under `packages/`
  because it is WS-F-specific (platform), but it is published as a stable
  consumption contract for WS-A/WS-C/WS-D (signature documented in
  `services/platform/README.md`).
- **Change type:** new package, not a change to an existing contract — does
  not require chief-architect approval for the initial v0.1.0, but future
  changes to `SecretsClient`'s public signature follow rule 2 above as soon
  as WS-A/WS-C/WS-D actually integrate.

## How to propose a breaking change

1. Open an issue/PR describing the affected field/signature, why an additive
   change is not sufficient, and every known consumer.
2. Tag the program's chief architect for review.
3. After approval, do the version bump + this changelog entry in the same PR
   as the contract change (before any PR that depends on the change).
