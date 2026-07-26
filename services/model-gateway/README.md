# WS-D — model-gateway (LiteLLM)

In-VPC model gateway for Fintex DSE. Every LLM call made by any agent session
(Coder in Phase 1; Planner/Tester/Reviewer in Phase 2) goes through here —
never through a provider SDK directly (`anthropic`, `boto3`, `openai`).
The consumption contract is already published by the foundation:
`dse_contracts.gateway_contract.{GatewayCallHeaders,GatewayErrorResponse,Stage}`.

## What is implemented and working (tested against REAL infra, no mocks)

- **Real LiteLLM proxy running in Docker** (`docker-compose.wsd.yml`, port
  4000), image pinned by **digest** (not by the floating `main-latest` tag):
  `ghcr.io/berriai/litellm@sha256:4c76cc4f47b72c82194f2774f458cc92de369ac6439b236757f0f69b71392722`
  (litellm==1.93.0, pulled on 2026-07-09). Config in `litellm_config.yaml`.
- **3 models registered in LiteLLM** and confirmed via `GET /v1/models`:
  `bedrock/anthropic.claude-3-5-sonnet`, `bedrock/anthropic.claude-3-haiku`
  (infra placeholders, see below) and **`eco/echo-model`** (working right
  now).
- **"Echo model"** (`echo_provider/server.py`): OpenAI-compatible HTTP server
  written from scratch, stdlib only (`http.server`), deterministic (same input
  → same output, always; no RNG, no clock), running as its own container
  (`model-gateway-echo`) on the `dse_net` network, registered in LiteLLM as
  provider `openai/echo-model` via `api_base`. Proves the gateway end-to-end
  with no paid/external API involved.
- **Real virtual keys**: `mint_virtual_key(tenant_id, work_item_id,
  stage)` / `revoke_virtual_key(key)` call LiteLLM's native API
  (`POST /key/generate` / `POST /key/delete`) against a **dedicated** Postgres
  (`dse_litellm`, same instance, separate database from the shared `dse`
  schema — created with `CREATE DATABASE dse_litellm OWNER dse;`, migrated
  automatically by LiteLLM itself via Prisma on first boot).
  Tested: mint → call with the virtual key works → revoke → the next call
  gets a real `401` from the real proxy.
- **Model-scoping of virtual keys** confirmed: a key issued with
  `models=["eco/echo-model"]` gets `403` when it tries to call
  `bedrock/anthropic.claude-3-haiku`.
- **`virtual_keys` table** (`migrations/0005_wsd.sql`, applied to the
  foundation's Postgres): the DSE-side record of every key issued/revoked per
  tenant/work_item/stage, with `key_hash` (sha256, the key is never stored in
  cleartext) so `revoke_virtual_key` can look it up without persisting a secret.
- **Audit ledger (P8)**: `virtual_key.issued`, `virtual_key.revoked`,
  `virtual_key.issue_failed`, `virtual_key.revoke_failed` — all via
  `dse_audit.emit(actor="system:model-gateway", ...)`, never a direct INSERT.
  Confirmed with a real query against the partitioned `audit_log`.
- **LiteLLM master key read from the real dev Vault** (`localhost:8200`, KV v2,
  path `secret/data/model-gateway/master-key`) with fallback to the
  `DSE_LITELLM_MASTER_KEY` env var if Vault is unreachable — this is not a
  fixture, it is a real HTTP read against the foundation's Vault (see
  `settings.py`).
- **OTel instrumentation (WSD-E3-T1)**: every `chat_completion` produces a
  `dse.model_gateway.chat_completion` span carrying the contract attributes
  (`dse.tenant_id/work_item_id/stage/model/cost_usd/tokens_in/tokens_out`),
  filled with the **real** cost/tokens LiteLLM returns (the
  `x-litellm-response-cost-original` header + `usage` from the body) — never
  recomputed on our side. The span is also marked status ERROR on failures
  (denials are visible in observability).
- **Cost export (WSD-E3-T2)**: `cost_export.aggregate_cost()` aggregates by
  `(tenant_id, task_class, stage)` from the spans; `export_api.py`
  exposes that as 2 FastAPI routes (`GET /internal/cost-export`,
  `GET /internal/cost-export/by-tenant`); `scripts/cost_export_cli.py` is the
  command-line version.
- **`model_gateway_client` published as an installable Python library**
  (its own `pyproject.toml`) — this is what `sandbox_runtime` (WS-C) imports.
- **Conformance test (WSD-E1-T4)**: static proof (AST — no
  `import boto3/anthropic/openai` in any file of the package) + dynamic proof
  (intercepts every `httpx.post` call during a real mint→call→revoke flow and
  confirms 100% of them went to the gateway base URL, never to another host).
- **"Simulated upgrade" smoke test (WSD-E1-T1)**:
  `scripts/smoke_test.py` records a deterministic baseline (the echo model's
  response) and compares byte-for-byte against it — the upgrade procedure is
  documented at the top of the script itself.

## Phase 2 ("Judgment & queue") — what was added (WSD-E2/E3-T4/E4/E5)

Everything below is ADDITIVE on top of Phase 1 (the 20 Phase 1 calls/surfaces
are unchanged). Enforcement is **permissive by default**: with no policy, no
cap, no kill switch and no reassignment configured, `chat_completion` behaves
exactly as in Phase 1. Migration: `migrations/0011_wsd2.sql`
(`model_policies`, `model_call_ledger`, `work_item_budgets`,
`gateway_kill_switches`, `model_reassignments`).

### WSD-E2-T1 — Per-stage/per-tenant policy engine (`policy.py`)
- **Declarative** config (outside the agent code) in the `model_policies` table,
  mapping `(tenant, stage, data_class, risk_class) -> {allowed_models,
  preferred_model}`. Wildcard `'*'` on any dimension; the most **specific** row
  (fewest wildcards), tie-broken by `priority`, wins. No applicable row ->
  allow-all (Phase 1 preserved).
- **Hot-reload without redeploy**: the engine reads the table at call time with a
  short TTL cache (`DSE_POLICY_CACHE_TTL_SECONDS`, default 5s). An operator runs
  an `INSERT/UPDATE` and the effect shows up within TTL seconds.
  `load_policies_from_file` loads a declarative YAML/JSON ("config as code")
  into the table.
- **Typed deny**: a call to a disallowed model -> `GatewayCallError` (HTTP
  403) with body `GatewayErrorResponse{error="policy_denied"}` + an audit row
  `gateway.call_denied_policy` (P8). The WS-B workflow turns this into Failed (P6).
- **Integration with the WS-F access bundle** (`dse_access_bundle`): if the
  tenant's default bundle exists and is `enabled=false`, the tenant is turned off
  (deny-all). Defensive read — if the WS-F table does not exist yet, it degrades
  to "no additional restriction".
- Note: `risk_class` is a dimension of the table but today it is always `'*'` at
  resolution time because `GatewayCallHeaders` (the foundation contract) does not
  yet carry a risk header — once it does, the engine already matches the
  dimension with no schema change.

### WSD-E2-T2 — Budget enforcement at call time (`budget.py`)
- Two caps checked on **every** call: the WorkItem runtime budget
  (`work_item_budgets` or `per_task_usd` from the access bundle) and the tenant's
  **aggregate** monthly budget (`tenant_config.monthly_budget_usd` from WS-F or
  `monthly_usd` from the access bundle). "spent-so-far" comes from the **durable
  ledger** (not from an in-memory counter).
- Exhaustion -> clean refusal at the boundary (P6): `GatewayCallError` (HTTP 402)
  `GatewayErrorResponse{error="budget_exhausted"}` + audit
  `gateway.call_denied_budget`.

### WSD-E4-T2 — Scoped kill switch + in-flight model reassignment (`controls.py`, `control_api.py`)
- Kill switch with **4 scopes** (global | tenant | work_item | channel). At call
  time the gateway enforces the scopes visible in the headers
  (global/tenant/work_item); `channel` lives in the table for operability but is
  honored at admission by WS-A/WS-B (the gateway does not see the channel).
- **Wired into the WS-B/WS-F controls**: the check ALSO reads
  `dse_kill_switch_global` (WS-F) and `tenant_config.kill_switch_enabled` (WS-F),
  so tripping it through any path zeroes out calls in that scope. Effect **<60s**:
  short TTL cache (default 5s), well under 60s (it does not interrupt a
  generation already in progress — there is no streaming here — it stops new
  calls in the scope from being issued).
- **In-flight model reassignment**: an operator swaps the effective model of a
  WorkItem; the next call uses `to_model` instead of the requested one.
  Reassignment **does not bypass policy** (the effective model still goes through
  the policy engine).
- `control_api.py` is the operator FastAPI (`uvicorn
  model_gateway_client.control_api:app --port 4010`): `POST /internal/kill-switch`,
  `POST /internal/reassign-model`, `DELETE /internal/reassign-model/{wi}`,
  `GET /internal/budget-status`, `GET /internal/policy`. Every mutation already
  emits audit via `controls.py`.

### WSD-E3-T4 — Cost aggregation from a DURABLE source (`ledger.py`)
- Every successful call writes a row to `model_call_ledger` with LiteLLM's
  **real** cost/tokens. `cost_export.aggregate_cost(source="ledger")` (the new
  **default**) reads from that table — it **survives restarts** and aggregates
  across processes (this closes open item #4 from the addendum).
  `source="memory"` keeps the legacy path (in-memory spans) for pure unit tests.
- The **WS-F OTel collector is already up** (`dse_otel_collector`, OTLP on
  `localhost:4317/4318`) — proven in this session: with
  `DSE_OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318/v1/traces` the
  `dse.model_gateway.chat_completion` spans reach the collector (visible in
  `docker logs dse_otel_collector`). The local collector only does `debug`/stdout
  (no queryable backend), which is why the **queryable** aggregation lives in the
  Postgres ledger; the spans in the collector are for WSF-E7 dashboards/alerting.
- `OTEL_ATTR_TASK_CLASS` now comes from `dse_contracts.constants` (promoted in
  the Phase 2 foundation); `telemetry.py` re-exports it for compatibility.

### WSD-E5-T1 — Tier-2 evaluation suite (`eval_suite/`) — **owner: WS-D**
- A real, runnable harness (`python -m model_gateway_client.eval_suite`) that
  fires reference prompts (`eval_suite/cases.yaml`) against the configured models
  and reports pass/fail + cost + latency per case. Models unavailable in the
  current infra (e.g. `bedrock/*` with no AWS) become **SKIP**, not failures.
  Exit code 0 if nothing failed (CI gate / model promotion gate). This is **not**
  the full air-gapped Tier-2 serving stack (Phase 4) — it is the minimal eval
  scaffolding (gap 6) with a named owner.

## Phase 3 ("Evidence") — what was added (WSD-E4-T1 + WSD-E4-T3)

Everything is ADDITIVE on top of Phases 1+2 (the previous 39 tests still pass
unchanged). No new migration: WS-D Phase 3 does not need a table of its own
(the reserved `migrations/0016_wsd3.sql` went unused — failover is declarative
config; degradation/failures become rows in `audit_log` via `dse_audit.emit`,
and cost still goes to the Phase 2 `model_call_ledger`).

### WSD-E4-T1 — Intra-tier failover and degradation

- **Second instance of the echo model** (`dse_model_gateway_echo_b`, same
  `local-dev` tier, `docker-compose.wsd.yml`) registered in LiteLLM as
  `eco/echo-model-b`. It exists to prove failover FOR REAL: the tests run
  `docker stop` on the primary container and the next call is served by B.
- **Native proxy fallback** (`router_settings.fallbacks` in
  `litellm_config.yaml`): `eco/echo-model -> [eco/echo-model-b]`, with
  `num_retries: 1`, `cooldown_time: 1` and, on the echo deployments, `timeout: 5` +
  `max_retries: 0` (a local connection failure is detected fast — deterministic
  failover). Declarative, outside the agent code (P1).
- **Strictly intra-tier (NFR-07/P2)**: no fallback route crosses the contracted
  tier. Automated negative test
  (`test_no_fallback_route_crosses_tier`) parses the proxy's real config and
  fails CI if any (primary, fallback) pair has a different `dse_tier`.
- **Degradation is never silent (P8)**: `model_gateway_client/failover.py`
  detects a fallback from the LiteLLM headers (`x-litellm-attempted-fallbacks`
  \> 0 + `x-litellm-model-api-base` = the endpoint that served it) and emits the
  audit row `gateway.call_degraded_fallback` with the serving endpoint, the
  candidates and the policy verdict for each of them. Cost/attribution stay
  correct: the `model_call_ledger` row goes out with the SAME
  tenant/work_item/stage/task_class.
- **Fallback does not bypass policy** (same rule as Phase 2 reassignment): if
  NONE of the model group's declared fallbacks is allowed by the tenant/stage
  policy, the degraded response is refused at the boundary (P6) with
  `policy_denied`/`kind=fallback_model_not_allowed` + audit — the real cost
  already incurred is still written to the ledger (honest accounting).
- **Both down => clean refusal (P6)**: typed error at the boundary
  (`GatewayCallError` 408/5xx with a clear message) + audit
  `gateway.call_failed_upstream`; the WS-B workflow treats it as an Activity
  boundary (Temporal automatic retry or Failed).
- **Declarative mirror on the client**: `failover.intra_tier_fallbacks()`
  (overridable via `DSE_INTRA_TIER_FALLBACKS`, JSON) mirrors the proxy's map;
  consistency is guaranteed by a test
  (`test_client_fallback_mirror_matches_litellm_config`). Call sites that mint
  scoped keys must use
  `mint_virtual_key(..., models=intra_tier_failover_set(model))`.
- **Honest empirical finding (LiteLLM 1.93.0)**: the router's fallback happens
  EVEN with a virtual key scoped to the primary model only — the key's
  model-scoping does NOT restrict the fallback target (verified in this session
  with a key `models=["eco/echo-model"]` being served by B). In other words, the
  Phase 2 server-side backstop does not cover the fallback path; that is why the
  policy check on the served model runs on the client. For a 100%
  non-bypassable setup the same Phase 2 open item applies: mirror the enforcement
  as a proxy pre-call hook (see "What is missing for production" #6).

### WSD-E4-T3 — Model-path chaos battery (extension)

The egress-fail-closed / key-expiry / gateway-oscillation scenarios ALREADY EXIST
in `services/orchestrator/tests/test_chaos.py` (WSB-E5-T3b) — they were **not
duplicated**. `tests/test_chaos_gateway.py` adds, against REAL infra:

- **Total provider outage** (docker stop on BOTH echoes): typed refusal at the
  boundary + audit `gateway.call_failed_upstream` + ZERO ledger rows (no phantom
  cost, no truncated output). Measured: ~50s until LiteLLM's final error with
  both down (connect-timeouts × retries × fallback).
- **Quota exhaustion (429 end-to-end)**: the echo returns a deterministic 429
  (marker `[[SIMULATE_QUOTA_EXHAUSTED]]` in the last user message — OpenAI error
  shape, see `echo_provider/server.py`); LiteLLM propagates RateLimitError; the
  client raises a clean `GatewayCallError(429)` + audit.
- **Intra-tier failover under failure**: `tests/test_failover_intra_tier.py`
  (T1 above — same set of changes).
- **Budget exhaustion mid-task**: cap $1.00; a call under the cap completes IN
  FULL; spend blows past the cap; the next call is refused at the BOUNDARY
  (402 `budget_exhausted` + audit) — zero truncation at any point (P6).
- **Egress to a non-allowlisted model endpoint** (failure mode 12), against
  WS-C's REAL egress proxy on `:8806`: `api.openai.com` (plain HTTP) => 403;
  `api.anthropic.com` (CONNECT tunnel) => proxy refuses; positive control — the
  ONLY allowed model route (`model-gateway:4000`, resolved inside the Docker
  network by the proxy itself) works through the SAME proxy. The tests skip with a
  clear message if the proxy is not up.
- **Upstream/transport failure audit (new, P8)**: `gateway_call.py` now
  emits `gateway.call_failed_upstream` (with status_code, model, truncated error
  body; `status_code=0` for transport errors) for EVERY failure coming from the
  gateway/provider — a failure is never just an exception that gets lost.

Contract requests (for whenever someone touches the foundation; nothing
blocking):
- No new field is needed in `dse_contracts` for this delivery. The degraded
  refusal body reuses `GatewayErrorResponse` with extras
  (`kind`/`fallback_candidates`), the same pattern as the Phase 2 denials.

## Stable public API (`model_gateway_client`)

```python
from model_gateway_client import mint_virtual_key, revoke_virtual_key
from model_gateway_client import chat_completion, ChatCompletionResult
from dse_contracts.gateway_contract import GatewayCallHeaders, Stage

key = mint_virtual_key(tenant_id, work_item_id, Stage.coder, models=["eco/echo-model"])

result = chat_completion(
    headers=GatewayCallHeaders(tenant_id=tenant_id, work_item_id=work_item_id, stage=Stage.coder),
    virtual_key=key,
    model="eco/echo-model",
    messages=[{"role": "user", "content": "..."}],
)
# result.content / result.cost_usd / result.tokens_in / result.tokens_out

revoke_virtual_key(key)
```

**`sandbox_runtime` (WS-C) must import exactly these two functions** from
`model_gateway_client` for the per-Coder-session key lifecycle — the signature
is stable and must not change without coordination:

```python
def mint_virtual_key(
    tenant_id: str,
    work_item_id: str,
    stage: Stage | str,
    *,
    models: list[str] | None = None,
    max_budget_usd: float | None = None,
    ttl_seconds: int | None = None,
) -> str: ...

def revoke_virtual_key(key: str) -> None: ...
```

## What is a local fixture/mock (and why)

- **Bedrock/PrivateLink**: there is no AWS account available in this development
  session. `litellm_config.yaml` registers the 2 Bedrock models with real
  `litellm_params` (the same shape production would use) but the values
  (`DSE_AWS_REGION`, `DSE_BEDROCK_PRIVATELINK_ENDPOINT`,
  `DSE_BEDROCK_IAM_ROLE_ARN`) are **placeholders** — see
  `docker-compose.wsd.yml`. LiteLLM registers the model_list without error
  (credential validation only happens on the first real call), so you can see the
  2 aliases in `GET /v1/models` even without AWS, but an actual call to
  `bedrock/*` would fail (expected) until the real infra exists.
- **Echo model**: it is not a real LLM — it is a deterministic double. It is
  "real" in the sense of being a real HTTP server running in a real container,
  spoken to over real HTTP by the real LiteLLM — only the model's "intelligence"
  is a string transformation, documented exactly that way in
  `echo_provider/server.py`.
- **Echo model token counting**: `len(text.split())` (whitespace) — not a real
  BPE tokenizer. Enough to prove that cost/tokens flow end-to-end through the
  observability pipeline; it is not a production metric.
- **`cost_export`**: aggregates from an `InMemorySpanExporter` (the current
  process's buffer). It works perfectly for the tests and for local
  demonstration, but it does not survive a restart nor aggregate across
  processes — see "What is missing for production".

## What is missing for production

1. **Real Bedrock/PrivateLink**: replace the 3 placeholders in
   `docker-compose.wsd.yml` (`DSE_AWS_REGION`, `DSE_BEDROCK_PRIVATELINK_ENDPOINT`,
   `DSE_BEDROCK_IAM_ROLE_ARN`) with the real values provisioned by the customer's
   infra team (PrivateLink VPC endpoint for `bedrock-runtime`, an IAM role
   assumable via IRSA/instance profile — never a static access key).
2. **Production Vault**: today we read from the foundation's **dev** Vault
   (`localhost:8200`, root token). In production this should come from
   `services/platform/` (WS-F) with a shared client, scoped Vault policies (not a
   root token), and rotation. `settings.py` already isolates that detail behind
   `litellm_admin_master_key()` — swapping the implementation there should not
   affect `virtual_keys.py`/`gateway_call.py`.
3. **LiteLLM database in production**: today it is `dse_litellm` on the same dev
   Postgres instance. In production it should be its own managed instance
   (dedicated RDS), not sharing hardware with the control plane.
4. **Real cost export**: today it is a per-process in-memory buffer.
   Production needs the WS-F OTel collector receiving via OTLP
   (`DSE_OTEL_EXPORTER_OTLP_ENDPOINT` is already supported in `telemetry.py`, the
   collector just has to exist) and `cost_export._iter_spans()` should become a
   query against that backend instead of the local buffer — `aggregate_cost()` is
   already the stable interface for that swap.
5. **`OTEL_ATTR_TASK_CLASS`**: I used an extra attribute (`dse.task_class`,
   `model_gateway_client.telemetry.OTEL_ATTR_TASK_CLASS`) that does not exist in
   `dse_contracts.constants` today (the published contract only has
   TENANT/WORK_ITEM/STAGE/MODEL/COST_USD/TOKENS_IN/TOKENS_OUT). I did not edit
   `packages/contracts` (out of my scope) — but for the per-task-class
   aggregation asked for in WSD-E3-T2 to make sense outside this process, that
   attribute should be promoted to the shared contract the next time someone
   touches `dse_contracts.constants`.
6. **Non-bypassable server-side enforcement** (Phase 2 shipped with an honest
   caveat): the policy/budget/kill-switch/reassignment logic in `enforcement.py`
   runs on the gateway's **client** path (`chat_completion`). The server-side
   backstop already exists and is NOT bypassable by the sandbox — virtual keys are
   model-scoped in LiteLLM (native 403, tested) and can carry
   `max_budget`/`duration`. For a 100% non-bypassable deployment, the same
   `enforce_call` must be mirrored as a **LiteLLM proxy pre-call hook**
   (custom callback loaded in `litellm_config.yaml` + rebuild of the pinned image)
   — not done in this session so as not to touch the digest-pinned image without
   running the upgrade smoke test. The logic is the same function; only the mount
   point changes.
7. **Automatic rotation/expiry of virtual keys** — today a key is only revoked
   explicitly via `revoke_virtual_key`. `ttl_seconds`/`max_budget_usd`
   are already accepted by `mint_virtual_key` and forwarded to LiteLLM
   (`duration`/`max_budget`), but the "revoke automatically when the work_item
   finishes" lifecycle is `sandbox_runtime`'s (WS-C) responsibility, not this
   package's.

## How to run the WS-D infra

The shared infra (Postgres/Temporal/Redis/Vault) is already up — do not run
`make up`/`make down`. To bring up ONLY the WS-D services (it uses the
external `dse_net` network already created by the foundation and does not affect
the other containers):

```bash
# LiteLLM's dedicated database (one time only, idempotent)
docker exec dse_postgres psql -U dse -d dse -c "CREATE DATABASE dse_litellm OWNER dse;" || true

docker compose -f docker-compose.wsd.yml up -d --build
```

Apply the `0005_wsd.sql` migration (idempotent):

```bash
DSE_DATABASE_URL=postgresql://dse:dse_dev_only@localhost:5432/dse python3 scripts/migrate.py
```

## How to run the tests

```bash
python3.12 -m venv /Users/saraiva/Documents/DSE/fase1/.venv-wsd
source /Users/saraiva/Documents/DSE/fase1/.venv-wsd/bin/activate
cd /Users/saraiva/Documents/DSE/fase1
pip install -e packages/contracts -e packages/dse_audit -e packages/dse_identity
pip install -e services/model-gateway
pip install pytest

cd services/model-gateway
pytest -q
```

`tests/conftest.py` already assumes the `docker-compose.wsd.yml` defaults
(`http://localhost:4000`, the dev master key, dev Vault, the foundation's
Postgres) via `os.environ.setdefault` — it works out of the box if the infra
above is running.

### Real result (run in this session)

Phase 1 (20) + Phase 2 (19) + Phase 3 (12) against the SAME real infra:

```
51 passed in 66.58s
```

(The runtime went up because the Phase 3 failover/chaos tests really do stop and
restart containers and wait for the primary to start serving again. The chaos
tests stop ONLY WS-D's own echo containers — never the shared infra — and
restore them in a `finally`.)

Phase 1 coverage: `echo_provider` in isolation (determinism, OpenAI shape, 404),
the full mint→call→revoke→denied round-trip against the real LiteLLM,
virtual-key model-scoping (403), the `virtual_keys` table (real insert/revoke in
Postgres), the audit ledger (real rows written/queried),
OTel telemetry (contract attributes + status ERROR on failure), cost
aggregation (multi-tenant, multi-task-class), and gateway-only
conformance (static + dynamic).

Phase 2 coverage:
- `test_policy_enforcement.py` — resolution by specificity/wildcard, permissive
  default, typed deny + audit on a real denied call, hot-reload changing the
  decision without a redeploy, loading policy from YAML.
- `test_budget_enforcement.py` — real cost accumulated in the durable ledger,
  WorkItem and tenant (`tenant_config`) budget denials at the boundary + audit,
  `budget-status`.
- `test_kill_switch_reassign.py` — work_item kill switch (on/off), the gateway
  honoring WS-F's tenant kill switch (`tenant_config`), reassignment swapping the
  effective model (echo instead of haiku) + audit, reassignment not bypassing
  policy.
- `test_ledger_durable.py` — durable aggregation survives clearing the in-memory
  buffer (a proxy for restart), per-tenant isolation.
- `test_eval_suite.py` — the echo cases pass, an unavailable model becomes SKIP.

Also verified by hand in this session (not in pytest): the real OTLP export
reaches WS-F's `dse_otel_collector` (1 `dse.model_gateway.chat_completion` span
received) and `python -m model_gateway_client.eval_suite` runs (3 pass, 1 skip).

Phase 3 coverage:
- `test_failover_intra_tier.py` — WSD-E4-T1: the client/proxy mirror of the
  fallback map is consistent; negative tier test (no route crosses
  `dse_tier`); healthy primary with no false positive for degradation; primary
  TAKEN DOWN (real docker stop) -> the fallback takes over with a complete
  response + correct attribution in the ledger + degradation audit; fallback does
  not bypass policy (403 `fallback_model_not_allowed` refusal + real cost still
  accounted for).
- `test_chaos_gateway.py` — WSD-E4-T3: total outage (both echoes down) ->
  typed refusal + audit + zero ledger rows; 429 quota end-to-end; budget
  exhaustion mid-task at the boundary; default-deny egress to public model
  endpoints through the REAL proxy on :8806 with a positive control on the only
  allowed route.
- `scripts/smoke_test.py` re-run after the LiteLLM config change
  (fallbacks/timeout): response identical to the baseline, byte for byte.

## Version pinning and simulated upgrade (WSD-E1-T1)

See the comment at the top of `docker-compose.wsd.yml` and the docstring of
`scripts/smoke_test.py`. In short: always pin the image by digest, never by a
floating tag; an upgrade = swap the digest + `docker compose ... up -d
--force-recreate model-gateway` + `pytest -q` + `scripts/smoke_test.py`
coming back clean against the baseline in `scripts/smoke_baseline.json` before
promoting.

## Files

```
migrations/0005_wsd.sql                        # virtual_keys table (Phase 1, repo root)
migrations/0011_wsd2.sql                        # Phase 2: model_policies, model_call_ledger,
                                               #   work_item_budgets, gateway_kill_switches,
                                               #   model_reassignments (repo root)
docker-compose.wsd.yml                          # model-gateway + model-gateway-echo services (repo root)
services/model-gateway/
  litellm_config.yaml                           # LiteLLM config (bedrock placeholders + real echo)
  pyproject.toml                                # installable model-gateway-client package (+pyyaml)
  README.md
  echo_provider/
    server.py                                   # "echo model" — pure stdlib, deterministic
    Dockerfile
  model_gateway_client/
    __init__.py                                 # stable public surface (Phase 1 + Phase 2)
    settings.py                                  # env vars + real read from the dev Vault
    db.py                                        # Postgres connection (dse control plane)
    virtual_keys.py                              # mint_virtual_key / revoke_virtual_key
    gateway_call.py                              # chat_completion (call-time enforcement + ledger)
    telemetry.py                                 # OTel spans (dse_contracts.constants contract)
    cost_export.py                               # cost aggregation (default: durable ledger)
    export_api.py                                # thin FastAPI over cost_export
    errors.py
    # --- Phase 2 ---
    policy.py                                    # WSD-E2-T1 per-stage/per-tenant policy engine
    budget.py                                    # WSD-E2-T2 call-time budget enforcement
    controls.py                                  # WSD-E4-T2 kill switch (4 scopes) + reassignment
    enforcement.py                               # single enforcement point (policy+budget+kill+reassign)
    ledger.py                                    # WSD-E3-T4 DURABLE cost ledger (Postgres)
    control_api.py                               # operator FastAPI (kill switch/reassign/budget/policy)
    # --- Phase 3 ---
    failover.py                                  # WSD-E4-T1 fallback mirror + degradation detection/audit
    eval_suite/
      __init__.py                                # WSD-E5-T1 Tier-2 eval suite (owner: WS-D)
      cases.yaml                                 # reference prompts + assertions
      runner.py                                  # harness (run_suite / run_case)
      __main__.py                                # CLI (model promotion gate)
  scripts/
    smoke_test.py                                # simulated upgrade
    smoke_baseline.json                           # baseline recorded in this session
    cost_export_cli.py
  tests/
    conftest.py
    test_echo_provider.py                         # Phase 1
    test_gateway_e2e.py                           # Phase 1
    test_virtual_keys_table.py                    # Phase 1
    test_audit_emission.py                        # Phase 1
    test_telemetry.py                             # Phase 1
    test_cost_export.py                           # Phase 1
    test_conformance_gateway_only.py              # Phase 1
    test_policy_enforcement.py                    # Phase 2 WSD-E2-T1
    test_budget_enforcement.py                    # Phase 2 WSD-E2-T2
    test_kill_switch_reassign.py                  # Phase 2 WSD-E4-T2
    test_ledger_durable.py                        # Phase 2 WSD-E3-T4
    test_eval_suite.py                            # Phase 2 WSD-E5-T1
    chaos_helpers.py                              # Phase 3: docker stop/start + recovery wait
    test_failover_intra_tier.py                   # Phase 3 WSD-E4-T1
    test_chaos_gateway.py                         # Phase 3 WSD-E4-T3
```
