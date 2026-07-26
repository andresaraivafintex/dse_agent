# Monorepo conventions — Fintex DSE, Phase 1 (Core loop)

Read by: every agent/engineer building inside `services/*`. This document is the "contracts
sprint" of weeks 1-2 of the master plan (D1, D2, D3, D5, D6, D7, D10, D11, D12): it defines what
is already built (the foundation) and the rules for not colliding with the other workstreams
while they work in parallel.

## Scope of this Phase 1 ("Core loop")

Per `plano-desenvolvimento/00-PLANO-MESTRE.md` §3: the full cycle Slack/GitHub →
clarification → **single Coder** in a sandbox → L1 → deterministic PR → human review → human
merge, durable and auditable. **Not part of Phase 1**: Planner/Tester/Reviewer split
(Phase 2), plan-approval gate by risk class (Phase 2), Jira (Phase 2), L2 fresh-context
review (Phase 2), Argo CD previews / Playwright evidence (Phase 3), skill registry (Phases 2/4).

## Stack

- **Language: Python 3.11+** across all services (containers use `python:3.11-slim`; not
  dependent on the host version). Rationale: the OpenHands SDK and LiteLLM are Python-native —
  this minimizes integration friction between the sandbox runtime and the model gateway.
- Packaging: `pyproject.toml` (PEP 621) + `setuptools`, no Poetry/uv (P7 — boring-first,
  one fewer tooling dependency).
- Data validation: **pydantic v2**.
- Durable orchestration: **Temporal** (Python SDK), self-hosted via local docker-compose.
- Database: **Postgres 16**. Plain, numbered SQL migrations (see below), applied by
  `make migrate` (a simple script in `scripts/migrate.py`, no migration framework).
- Tests: **pytest**, with fixtures that run against the Postgres/Temporal from
  `docker-compose.yml` (never mocks for the durability/idempotency guarantees — those are the
  whole point of the system).
- HTTP: **FastAPI** for any service that receives webhooks.

## Directory ownership (no agent edits outside its scope)

| Directory | Workstream | Contents |
|---|---|---|
| `packages/contracts/` | Foundation (do not edit without notifying the architect) | `ConversationEvent`, `WorkItem`/`DseTaskRequest`/`DseTaskStatus`, `PlanArtifact` (stub), gateway consumption contract, single-mutable-comment-per-surface library |
| `packages/dse_audit/` | Foundation (minimal) → **WS-F extends** | Audit ledger write client + reconstruction/export queries |
| `packages/dse_identity/` | Foundation (minimal) → **WS-F extends in Phase 2** | `platform_user_id` → single principal resolution |
| `services/adapter-slack/` | **WS-A** | Inbound (mentions/replies/buttons) + outbound (single status message edited in place) |
| `services/adapter-github/` | **WS-A** | Inbound (issues/PR comments) + outbound (single status comment) via GitHub App |
| `services/ingest-gateway/` | **WS-A** | Transactional gateway (outbox), dispatcher (`SELECT…FOR UPDATE SKIP LOCKED` → `StartWorkflow`), 4 defenses (signature, TOCTOU snapshot, sanitization, idempotency), Path A/B correlation, steering allowlist fallback |
| `services/orchestrator/` | **WS-B** | Temporal worker, §9.3 state-machine workflow, pause points, budgets, checkpoint/recovery, operator controls, chaos suite |
| `services/sandbox-runtime/` | **WS-C** | Sandbox lifecycle (provision/teardown/checkpoint) as Activities, rootless Docker driver, substrate interface + OpenHands adapter, Coder session |
| `services/egress-proxy/` | **WS-C** (WS-F signs off on the policy) | Default-deny proxy + ephemeral credential injection |
| `services/model-gateway/` | **WS-D** | LiteLLM config, Bedrock/PrivateLink tier as an allowlist entry, virtual keys per tenant/task/stage, consumption contract |
| `services/validation/` | **WS-E** | L1 pipeline (lint/typecheck/test/build + SAST/secret-scan + diff-budget/forbidden-paths), idempotent PR finalizer, minimal status-check consumption (sliced L3) |
| `services/platform/` | **WS-F** | Vault/ESO wiring, IaC skeleton (`infra/`), per-tenant fairness/budget parameters, isolation-suite scaffolding, observability |

**Golden rule:** each workstream only creates/edits files inside its own directory (plus the
migration file and docker-compose fragment reserved below). If you need something in
`packages/contracts` that does not exist, add a new field/type without removing or renaming
what already exists — the public functions and classes listed in this document are a stable
contract.

## Commit hygiene — one workstream = one commit

Rules born out of the slicing sprint (plano 09, 2026-07-23), when ~2,900 lines
from 6 distinct workstreams piled up in a single working tree:

- **One line of work = one commit** (feature, operational fix, infra
  hardening, i18n — each one separate). Revert and `git bisect` are part of
  the system design, not a luxury.
- **A file the CI references is NEVER left untracked**: if `ci.yml`, the Helm
  chart, or the test matrix points at a file, it goes into the SAME commit
  that created the reference (a clean clone must always pass CI).
- **A fixture generated/mutated by a test is never tracked** — regenerate it
  with idempotent code (the `ensure_repo` pattern from the preview gitops) and
  gitignore the directory.
- A cosmetic change (i18n, mass rename) never lands in the same commit as a
  behavior change.

## Migrations — reserved numbering (avoids collisions in parallel)

`migrations/0001_foundation.sql` already exists (work_items, ingest_events, partitioned
append-only audit_log, principals/identity_links). If your workstream needs its own table in
Phase 1, use exclusively the file below (do not edit 0001):

| File | Workstream |
|---|---|
| `migrations/0002_wsa.sql` | WS-A |
| `migrations/0003_wsb.sql` | WS-B |
| `migrations/0004_wsc.sql` | WS-C |
| `migrations/0005_wsd.sql` | WS-D (e.g. issued virtual keys table) |
| `migrations/0006_wse.sql` | WS-E (e.g. validation runs table) |
| `migrations/0007_wsf.sql` | WS-F (e.g. tenant_config — budgets/fairness/kill switches) |

Run `make migrate` to apply all migrations in order (idempotent — it uses a `schema_migrations`
table so nothing is reapplied).

**Numbering (plano 09, Phase 4):** the numeric prefix is UNIQUE — CI fails on a
new collision (`tests/test_ci_tooling.py::test_migration_numeric_prefixes_are_unique`).
The historical `0020_wsc4`/`0020_wse4` collision is frozen (already applied in
real environments; never renumber an applied migration). Before creating a
migration, use the lowest free number above the highest existing one.

## docker-compose — each workstream writes its own fragment

`docker-compose.yml` (foundation) already brings up: `postgres`, `temporal` (+ `temporal-ui`),
`redis`, `vault` (dev mode). **Do not edit this file.** If your service needs to run in a
container, create `docker-compose.wsX.yml` (e.g. `docker-compose.wsa.yml`) with only your own
services, attached to the external network `dse_net` (already declared in the foundation). The
`Makefile` already merges every existing fragment in `make up`.

Reserved ports (avoid conflicts):

| Port | Service |
|---|---|
| 5432 | Postgres |
| 7233 / 8088 | Temporal frontend / Temporal UI |
| 6379 | Redis |
| 8200 | Vault (dev) |
| 4000 | LiteLLM (model-gateway, WS-D) |
| 8801 | adapter-slack (WS-A) |
| 8802 | adapter-github (WS-A) |
| 8803 | ingest-gateway (WS-A) |
| 8805 | sandbox-runtime control API (WS-C, if exposed) |
| 8806 | egress-proxy (WS-C) |
| 8807 | validation / PR finalizer webhook receiver (WS-E, if exposed) |
| 8900 | orchestrator health endpoint (WS-B) |

## Already-published contracts (do not reinvent — import)

- `dse_contracts.conversation_event.ConversationEvent` — the single normalized event every
  adapter produces (FR-01/§10.2). Fields: `event_id` (sha256 platform+thread+message),
  `platform`, `kind` (`task_request|clarification_answer|approval|review_comment|steering`),
  `source_ref` (thread_ts/ticket/pr), `actor` (platform_user_id + resolved principal),
  `content_snapshot`, `received_at`, `signature_verified`.
- `dse_contracts.work_item.WorkItem`, `DseTaskRequest`, `DseTaskStatus` — the §10.3 schema and
  the public API (coarse status: `running|blocked|done|failed`) of FR-01-04 in the table.
- `dse_contracts.plan_artifact.PlanArtifact` — plan artifact stub (steps, files,
  diff_budget, test_plan, risk_class) — already used in Phase 1 by WS-E's diff-budget
  enforcement even without a separate Planner session (the Phase 1 Coder fills in a minimal
  `PlanArtifact` before implementing).
- `dse_contracts.gateway_contract` — model-gateway consumption contract: single base URL,
  required headers (`tenant_id`, `work_item_id`, `stage`, `task_class`, `data_class`),
  error format for policy/budget refusals.
- `dse_contracts.mutable_comment.MutableCommentWriter` — shared library for
  "exactly 1 status comment/message per surface, edited in place, crash-consistent"
  (WSA-E3-T2/E4-T2, reused by WSE-E3-T7). The WS-A adapters and the WS-E PR finalizer use the
  same class with different back-ends (Slack API / GitHub API / Jira API).
- `dse_audit.client.emit(actor, action, work_item_id, tenant_id, details)` — the only write
  path into the audit ledger. Never write to `audit_log` outside this function.
- `dse_identity.resolve_principal(platform, platform_user_id, display_name=None)` — resolves
  (and, if necessary, creates) the single principal for a user seen for the first time on a
  platform. Phase 1: simple auto-registration resolution (no SSO/SCIM — that is ADR-22,
  Phase 2/WSF-E3-T3). Every surface must call this before writing `actor` anywhere.

## Non-negotiable principles (P1-P8 from the proposal) — all code must respect them

- **P1 deterministic-or-human**: no flow decision (approve, merge, open a PR, transition state)
  is made by an LLM. Deterministic code or a named human, always.
- **P3 no producer approves its own work**: no agent session may approve/merge its own diff.
- **P6 decline-never-truncate**: budget/cap exceeded → clean failure at a boundary, never a cut
  mid-stream. Never "silence" an error.
- **P8 evidence over assertion**: every consequential decision produces a line in the audit
  ledger.

## Python environment for building/testing in parallel

The foundation already validated `packages/*` in a venv at `.venv/` (Python 3.12, via
`python3.12 -m venv .venv`). **Each workstream creates its own isolated venv**
(`.venv-wsa`, `.venv-wsb`, `.venv-wsc`, `.venv-wsd`, `.venv-wse`, `.venv-wsf`
at the repo root) to install its dependencies and run `pytest` without
interfering with another workstream's concurrent installs — 6 builds are
running in parallel right now. Example:

```
python3.12 -m venv .venv-wsa
source .venv-wsa/bin/activate
pip install -e ../../packages/contracts -e ../../packages/dse_audit -e ../../packages/dse_identity  # adjust the path
pip install -e .   # your own service's pyproject.toml
pip install pytest
pytest -q
```

Infra is already up (`docker compose up -d` has already run: Postgres at `localhost:5432`
with migration `0001_foundation.sql` applied, Temporal at `localhost:7233`,
Redis at `localhost:6379`, Vault dev at `localhost:8200` with root token
`dse_dev_root`). Do not run `make up`/`make down` yourself (that would tear down the infra
for the other workstreams running in parallel) — just connect to it. If you
need your own table, write it into `migrations/000X_wsY.sql`
(your reserved number) and apply it yourself with
`DSE_DATABASE_URL=postgresql://dse:dse_dev_only@localhost:5432/dse python3 scripts/migrate.py`
(idempotent, only applies what is new).

## Phase 2 ("Judgment & queue") — scope and reservations

Phase 1 is complete and integrated (see `docs/PHASE1-STATUS.md` and the addendum
`../plano-desenvolvimento/01-ADENDO-FASE2-POS-FASE1.md`). Phase 2 adds:
Planner/Tester/Reviewer split (WS-C), plan-approval gate by risk class +
rejection path + budgets (WS-B), Jira adapter + tenant mapping +
merge webhook + signal routing by status (WS-A), policy/budget at call time
+ gateway kill switch (WS-D), L2 fresh-context (WS-E), access
bundles + ADR-22/SSO design + multi-tenant isolation suite + queue board
(WS-F). **Still out of scope until Phase 3/4:** Argo CD previews,
Playwright/video evidence, artifact store, skill promotion (only registry
bootstrap in Phase 2).

New contracts already published in the foundation (import, do not redefine):
`SIGNAL_PLAN_APPROVAL` (payload documented in `constants.py`),
`ACTIVITY_RUN_PLANNER_TURN` / `ACTIVITY_RUN_TESTER_TURN` /
`ACTIVITY_RUN_L2_REVIEW`, `L2Verdict`, `PrRef.compare_url` (optional,
`pr_number` now optional — exactly one of the two present),
`OTEL_ATTR_TASK_CLASS`.

Phase 2 reserved migrations (same rule as Phase 1 — one file per WS):

| File | Workstream |
|---|---|
| `migrations/0008_wsa2.sql` | WS-A (e.g. tenant_platform_bindings) |
| `migrations/0009_wsb2.sql` | WS-B |
| `migrations/0010_wsc2.sql` | WS-C (e.g. skill_registry, retrieval index) |
| `migrations/0011_wsd2.sql` | WS-D (e.g. model_policies) |
| `migrations/0012_wse2.sql` | WS-E |
| `migrations/0013_wsf2.sql` | WS-F (e.g. dse_access_bundles) |

Newly reserved ports: **8890** = admin console queue board (WS-F).

Infra note: the foundation's Temporal cluster was upgraded from
`auto-setup:1.24` to the highest version available in the registry (upgrade
drill WSB-E1-T5). Native Priority & Fairness (1.31+) is NOT available —
fairness in Phase 2 is worker-side (per-tenant concurrency caps read from
`tenant_config`), behind a swappable interface for when the server supports it.

## Phase 3 ("Evidence") — scope and reservations

Phases 1+2 complete and integrated (399 tests — see `docs/PHASE2-STATUS.md` and the addendum
`../plano-desenvolvimento/02-ADENDO-FASE3-POS-FASE2.md`). Phase 3 adds: full L3
(reflection + targeted re-runs), per-PR preview environments via Argo CD ApplicationSet,
Playwright `@demo` video, Garage artifact store (expiring links + quarantine + access
log), visual diff, evidence debounce (ADR-26), Playwright in the sandbox image +
`demos/<workitem-id>/` convention (WSC-E3-T4b), second substrate (Claude Agent SDK),
intra-tier failover + full chaos battery, complete ADR-28 (scheduled rotation +
preview secrets via ESO), retention by classification. **Out of scope until Phase 4:**
skill promotion, merge-base hardening, red-team.

**Entry gate ALREADY EXECUTED by the foundation:**
- Session models (Planner/Tester/L2) PROMOTED to `dse_contracts.activities`
  (`sandbox_runtime.activities` re-imports them) with boundary regression tests in
  `packages/contracts/tests/test_activity_boundaries.py` — **new rule: when you change a call
  site in the workflow, update the corresponding payload in those tests IN THE SAME PR.**
  `RunL2ReviewInput` now has `extra="forbid"` (structural P3 at decode time).
- Phase 3 evidence contracts ALREADY DEFINED in the foundation (import, do not redefine):
  `ACTIVITY_RUN_DEMO_EVIDENCE`/`PUBLISH_ARTIFACT`/`TRIGGER_PREVIEW`/`RUN_VISUAL_DIFF` +
  models `RunDemoEvidenceInput`/`DemoEvidenceResult`/`PublishArtifactInput`/`ArtifactRef`/
  `TriggerPreviewInput`/`PreviewRef`/`RunVisualDiffInput`/`VisualDiffResult`.
- **Local K8s cluster up**: k3d `dse-preview` (2 nodes, network `dse_net` — pods reach
  Vault/model-gateway/Garage by container name) with **Argo CD v2.13.3** installed and
  Available in the `argocd` namespace (ApplicationSet controller included). Idempotent setup:
  `infra/k8s-local/setup-k3d-argocd.sh`. kubecontext: `k3d-dse-preview`. ESO is installed
  by WS-F via helm (command at the bottom of the script). DO NOT delete the cluster.

Phase 3 reserved migrations: `0014_wsb3.sql`, `0015_wsc3.sql`, `0016_wsd3.sql`,
`0017_wse3.sql`, `0018_wsf3.sql` (WS-A has no tasks in Phase 3).

Newly reserved ports: **3900/3903** = Garage S3 API/admin (WS-E declares the service in
`docker-compose.wse.yml` — the fragment does not exist yet, create it); **8091** = Argo CD UI
port-forward (on demand, not fixed).

## How to run locally

```
make up        # brings up infra (postgres, temporal, redis, vault) + each workstream's fragments
make migrate   # applies all migrations in order
make test      # runs the test suite for all services (requires `make up` running)
make down      # tears everything down
```
