# services/sandbox-runtime (WS-C)

Per-task ephemeral sandbox (rootless Docker), lifecycle exposed as Temporal
Activities, agent substrate interface + OpenHands adapter, and a Coder session
with scope-limited git. See `CONVENTIONS.md` at the monorepo root for the
cross-workstream contract.

## What is implemented and working (tested against real Docker/Postgres)

### WSC-E1 — Per-task ephemeral sandbox

- **T1 — Isolation** (`docker_driver.py`): every sandbox container runs with
  `--user <non-root uid>`, `--read-only` + `--tmpfs /tmp`, `--cap-drop ALL`,
  `--security-opt no-new-privileges`, no `/var/run/docker.sock` bind, and is
  attached exclusively to the internal Docker network `dse_sandbox_net`
  (`internal=True` — no internet gateway). The only host reachable from
  inside the sandbox is the egress-proxy (`services/egress-proxy`), which in
  turn is attached to both `dse_sandbox_net` and `dse_net` (the latter has
  Docker's default internet route).
  Proved by `tests/test_network_isolation.py`, which brings up real Docker
  containers (sandbox + egress-proxy + an "upstream" standing in for the
  internet) and proves: (a) no mount references `docker.sock`; (b) `id -u`
  inside the sandbox is not `0`; (c) a direct request to an external host
  fails; (d) a request through the egress-proxy to an allowed host succeeds;
  (e) a request through the egress-proxy to a host outside the allowlist
  returns `403`.
- **T2 — Resource caps + OTel metrics** (`docker_driver.ResourceCaps`,
  `metrics.py`): `--cpus`/`--memory`/`--pids-limit` derived from the
  `WorkItem`'s `budget` (keys `cpu_limit`/`memory_mb`/`pids_limit`/
  `resource_class`, with per-`resource_class` defaults for `small`/`medium`/
  `large`). `teardown_sandbox` emits an OTel histogram
  `dse.sandbox.runtime_minutes` carrying the attributes
  `dse_contracts.constants.OTEL_ATTR_TENANT/WORK_ITEM/STAGE` +
  `dse.resource_class`. Proved by
  `tests/test_resource_caps_and_metrics.py` (inspects the container's real
  `HostConfig` and reads the data points through the OTel SDK's
  `InMemoryMetricReader`).
- **T3 — Lifecycle as Temporal Activities** (`activities.py`):
  `provision_sandbox`/`checkpoint_sandbox`/`rebuild_sandbox`/
  `teardown_sandbox` decorated with `@activity.defn(name=ACTIVITY_*)` using
  the exact names from `dse_contracts.activities`, returning
  `SandboxHandle`/`CheckpointRef`. Idempotency: `provision_sandbox` looks up
  the `dse.work_item_id` label before creating — called twice for the same
  `work_item_id`, it reuses the same container (proved by
  `tests/test_idempotent_provision.py`). `tests/test_temporal_activity_wiring.py`
  proves these are genuine Temporal SDK Activities (names match the contract,
  they execute through `temporalio.testing.ActivityEnvironment` — the SDK's
  official harness for testing an Activity in isolation, not a mock).
- **T4 — Checkpoint/rebuild + chaos** (`git_checkpoint.py`,
  `scoped_git.py`): a checkpoint is a commit + push of the task branch to a
  local bare git repo (`git init --bare`, acting as a test "origin" — not a
  real remote, as the brief allows). Rebuild clones the bare repo and does a
  `git checkout` of the checkpoint sha into a fresh workspace.
  `tests/test_checkpoint_chaos.py` kills the container mid-flight
  (`docker kill`) and proves that rebuild recovers the commits with no loss.

### WSC-E3 — Agent substrate + Coder session

- **T1 — Interface + adapters** (`substrate.py`): `AgentSubstrate` is a
  `Protocol` with `create_session`/`run_turn`/`collect_artifacts`.
  `FakeSubstrate` is a deterministic in-memory adapter (scripted per turn)
  used by every test — it makes no network/model calls at all.
  `OpenHandsSubstrate` is the real adapter on top of the `openhands-sdk` PyPI
  package (`pip install openhands-sdk` **worked in this session**, v1.21.0 —
  see "Known limitations" below for what is still missing to exercise a real
  turn). The OpenHands LLM is always constructed with
  `base_url=<model-gateway>` + `api_key=<virtual key>` +
  `extra_headers=GatewayCallHeaders(...).to_http_headers()` — it never points
  at a provider SDK/endpoint directly.
- **T2 — `run_coder_turn` with scope-limited git**
  (`activities.py::run_coder_turn`, `scoped_git.py`): the substrate ONLY
  edits files in the workspace — the commit/push to the task branch happens
  afterwards, in deterministic code (`ScopedGitSession`), never by the LLM
  (P1). Two enforcement layers against force-push/PR/wrong-branch:
  1. **Toolset**: `ScopedGitSession` exposes only `.commit()`/`.push()`
     (hardcoded refspec), with no `run_git_command`/`create_pull_request`/
     force-push.
  2. **Remote scope**: a REAL `pre-receive` hook (`install_pre_receive_guard`)
     installed on the checkpoint bare repo refuses any ref outside the task
     branch and any non-fast-forward — even if someone bypasses
     `ScopedGitSession` and runs a raw `git push --force`.
  3. **Credential scope**: `egress_proxy.credentials.ScopedCredential` never
     holds `pull_requests:write`/force — `create_pull_request()`/
     `force_push()` always raise `GitHubScopeError`.
  Proved adversarially by `tests/test_run_coder_turn_scoped_git.py` (raw
  force push → rejected by the hook; push to another branch → rejected;
  `ScopedGitSession.push()` propagates the refusal as `GitScopeViolation`).

## Phase 2 ("Judgment & queue") — what Phase 2 added (WSC-E3-T3/T4/T5, E4, E5)

A natural extension of the Phase 1 foundation (`AgentSubstrate` +
`ScopedGitSession` + Temporal Activities). Own migration:
`migrations/0010_wsc2.sql` (`skill_registry`, `retrieval_documents`). New
modules: `skill_registry.py`, `retrieval.py`, `toolsets.py`, `sessions.py`,
plus 3 new Activities in `activities.py` (in the `ACTIVITIES` list that the
WS-B worker imports).

### WSC-E4-T1 — Skill registry bootstrap (`skill_registry.py`)

Tenant-scoped `skill_registry` table, seeded with human-curated skills
(`created_by` = human principal, never `system:*`).
`read_approved_skills(tenant_id, task_class=…)` is the API the Planner reads:
it only returns `status='approved'` rows for the requested tenant — drafts
(`draft`) and skills from another tenant NEVER leak (isolation hardcoded in
the query; proved by `tests/test_skill_registry.py`, including two dynamic
tenants sharing the same `skill_key`). NO promotion pipeline (that is Phase 4)
— just the registry and the read path, exactly as scoped.

**Per-repository skills + workspace materialization** (migration 0029,
integration with the `dse_console_pane` console): the console is the central
skill store (SKILL.md format, synced from the `dse_skills` repo) and writes
the *per-repo ticks* into `skill_registry.repo_scope` (`NULL`=global,
`["*"]`=all, `["owner/name",…]`=those, `[]`=none).
`read_approved_skills(..., repo=…)` applies the filter; the **Planner** turn
materializes the served skills into the workspace's
`.claude/skills/<key>/SKILL.md` (`skill_files.py`) — skills already COMMITTED
in the target repo take precedence and none of this ends up in the diff
(`.git/info/exclude`). The **Coder** loads them natively
(`ClaudeAgentSubstrate` with `setting_sources=["project"]` — hermetic with
respect to the host) and the **Tester** gets the `workspace_skills_note` in
its prompt. Proved by `tests/test_skill_files.py` and
`tests/test_skill_repo_scope.py`.

### WSC-E5 — Retrieval/index service (`retrieval.py`, ADR-24)

`RetrievalService` on top of `retrieval_documents` (tenant-scoped), with three
capabilities over the same index: **repo map** (files + top-level symbols
extracted by a light multi-language regex), **lexical search** (BM25) and
**self-hosted embeddings** (sparse TF-IDF + cosine — no GPU in this session;
see "What is missing for production"). `index_repo` is idempotent (upsert by
`content_sha`). **STRICT TENANT ISOLATION**: `_require_tenant` refuses an
empty tenant; every query filters on `tenant_id = %s`; there is no
cross-tenant read path and no "list all" (proved by
`tests/test_retrieval.py::test_tenant_isolation_strict` — one tenant's index
is invisible to another; coordinated with the WS-F isolation suite).
**Indexed content is UNTRUSTED Planner input**: `RetrievalHit.trusted` is
always `False` and `render_untrusted_context` wraps the snippets in a clearly
delimited block instructing the model to treat them as DATA, never as
commands (defense against prompt injection coming from indexed code/tickets;
the Planner is read-only, so not even a malicious payload can trigger a
write).

### WSC-E3-T3 — Read-only Planner session (`run_planner_turn`, `sessions.py`, `toolsets.py`)

Activity `run_planner_turn` (name `ACTIVITY_RUN_PLANNER_TURN`): read-ONLY
toolset (`PlannerToolset`). It hydrates AGENTS.md + CODEOWNERS (from the
workspace), the tenant's approved skill registry (E4), related tickets and the
retrieval/index (E5), and emits a structured `PlanArtifact` (steps,
expected_files, diff_budget_lines, test_plan, risk_class). **P1**: the
`risk_class` — which drives the WS-B gate — is DERIVED by
`classify_risk_class` (deterministic code over the declared blast radius:
forbidden_paths, high-risk globs such as `**/*auth*`/`**/migrations/*`, diff
size), NOT by the LLM's word; the proposer (LLM) only suggests
steps/expected_files/test_plan. **Conformance** (proved by
`tests/test_planner_session.py`): any WRITE tool in the Planner FAILS with
`ToolPermissionError` (the session routes every tool call through
`Toolset.check` before dispatching — the test runs an `exploration_script`
containing a `write_file` and proves that it raises and that the file is never
created).

### WSC-E3-T4 — Tester session (`run_tester_turn`)

Activity `run_tester_turn` (name `ACTIVITY_RUN_TESTER_TURN`): `TesterToolset`
allows reads + `run_tests` + `write_file` ONLY under test paths (`tests/`,
`test_*.py`, `*_test.py`, `conftest.py`, `*.test.ts`, `_test.go`); writing to
production code FAILS (`ToolPermissionError`). The authored tests actually
EXECUTE — `run_tests` runs real `pytest` inside the workspace and reports
pass/fail (a failing test is reported as a failure, proving the execution is
real, not simulated). The commit/push of the test files is deterministic
(`ScopedGitSession`, identity `dse-tester`), never done by the LLM (P1).
Proved by `tests/test_tester_session.py`. Return value: `TesterTurnResult`
(not in `packages/contracts` because WS-C does not edit the foundation — see
"Gaps" below).

### WSC-E3-T5 — Fresh-context Reviewer session (`run_l2_review`)

Activity `run_l2_review` (name `ACTIVITY_RUN_L2_REVIEW`, returns `L2Verdict`
from `dse_contracts`): a FRESH session (`FreshReviewerSession`) that receives
ONLY the `ReviewerContext(plan, diff)` — **NOTHING from the Coder's history
(P3)**. The proof is BY CONSTRUCTION (`tests/test_reviewer_fresh_context.py`):
the fields of `ReviewerContext` and of the `RunL2ReviewInput` input are
exactly `{plan, diff}` (+ ids/classes) — there is no field or parameter that
could carry the producer's transcript, turns, thoughts or tool calls; the
fresh session only exposes `read_plan`/`read_diff` (no
`repo_map`/`search_code`/history). It returns an `L2Verdict` (passed +
specific file/line objections). The L2 verdict is a RECOMMENDATION that gates
progression (WS-E orchestrates the fix-retry loop around it) — the merge
remains human (P1), and because the session is fresh it is never the producer
approving its own work (P3).

### Phase 2 tests (real, against Postgres/Docker/Temporal SDK)

`test_skill_registry.py` (5), `test_retrieval.py` (7),
`test_planner_session.py` (5), `test_tester_session.py` (4),
`test_reviewer_fresh_context.py` (6) — **27 new tests**, all against real
infrastructure (Postgres for the registry/index; Docker for the sandbox
workspace of the Planner/Tester sessions;
`temporalio.testing.ActivityEnvironment` to prove the 3 new Activities are
genuine Temporal Activities carrying the contract names). **Real result of
this session: `42 passed` in sandbox-runtime** (15 from Phase 1 + 27 from
Phase 2) + `13 passed` in egress-proxy = **55 passed, 0 failed, 0 skipped**.

### Phase 2 gaps (documented, not hidden)

- **Real substrate for the Planner/Tester sessions**: as in Phase 1 (Coder),
  the scripted substrate (`ScriptedAgentSession`) is what the tests use — it
  does not call an LLM. In production the OpenHands adapter registers only the
  tools whose names are in the toolset allowlist and routes every tool call
  through the same `Toolset.check`, and the Planner's proposer / the
  Reviewer's `verdict_fn` become outputs of a fresh OpenHands `Conversation`.
  Wireable through `_run_planner_turn_impl(..., proposer=…)`,
  `_run_tester_turn_impl(..., authoring_script=…)`,
  `_run_l2_review_impl(..., verdict_fn=…)` — the same injection points as
  Phase 1's `_run_coder_turn_impl`.
- **Embeddings**: sparse TF-IDF (self-hosted, no GPU) is the "small local
  model" the brief allows. Swapping in a dense encoder (e.g.
  `sentence-transformers/all-MiniLM-L6-v2` on CPU) is ADDITIVE — same
  `EmbeddingModel` interface, same `embedding` column (JSONB). Documented at
  the top of `retrieval.py`.
- **`TesterTurnResult` outside the contract**: WS-C cannot edit
  `packages/contracts`. `run_tester_turn` returns a local `pydantic.BaseModel`
  (`activities.TesterTurnResult`); WS-B consumes it via dict/`model_validate`.
  The proposal to promote it into the contract (additive) is left to the
  architect's next window (CONTRACTS-CHANGELOG rule). `PlanArtifact` and
  `L2Verdict` were already in the contract.
- **`egress-proxy` (WS-C)**: unchanged in Phase 2 — the WS-C Phase 2 scope
  (E3-T3/T4/T5, E4, E5) lives entirely in `sandbox-runtime`. The Phase 1
  default-deny proxy + ephemeral credential injection remains valid and is the
  network route for the new sessions (LLM always via the model-gateway).

## Phase 3 ("Evidence") — what Phase 3 added (WSC-E3-T4b, WSC-E3-T6)

No new migration (`0015_wsc3.sql` was reserved but NOT created — no new table
was needed) and no change to the egress-proxy (the Playwright toolchain is
installed at image BUILD TIME; at runtime the sandbox still has no internet —
no new allowlist entries).

### WSC-E3-T4b — Playwright in the sandbox + `demos/<work_item_id>/` convention (P0)

- **(a) Base image with a real Playwright toolchain**
  (`docker/Dockerfile.sandbox-base`): base pinned to
  `python:3.11-slim-bookworm` (the `-slim` tag moved to trixie, which
  `playwright install --with-deps` does not support — a real failure observed
  and documented in the Dockerfile), node/npm from bookworm apt,
  `@playwright/test@1.49.1` + `playwright@1.49.1` pinned (P7) under
  `/opt/dse-playwright`, headless chromium via
  `npx playwright install --with-deps chromium` into
  `PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers` (world-readable — the runtime
  uid is a random non-root one), `HOME=/tmp`, and a `/node_modules` symlink →
  toolchain (npm 9's `npx` resolves by local prefix, not by `$PATH`; without
  the symlink it would try to INSTALL from the registry — impossible with no
  internet in the sandbox; a real failure observed and covered by tests).
  **Image genuinely built in this session: `dse-sandbox-base:wsc3`, 2.35GB**
  (the cost of chromium + system deps; the Phase 1 image
  `python:3.11-slim`+git remains the default for sessions that do not produce
  evidence — the image is a `provision_sandbox` parameter).
- **(b) `demos/<work_item_id>/` convention in the TesterToolset**
  (`toolsets.py::demo_dir_for/is_demo_path`): an additional ALLOWED write
  path, SCOPED to the session's work item. Hardening discovered by testing:
  inside `demos/` the generic test-path rule (`*.spec.js` anywhere, inherited
  from Phase 2) does NOT apply — otherwise one task's Tester could write into
  another task's demo simply by naming the file `*.spec.js`. Blocked and
  proved: production code, ANOTHER work item's `demos/`, path traversal
  (`demos/<wi>/../../src/...`), and any write under `demos/` in a session with
  no work item (`tests/test_tester_demo_convention.py`).
- **(c) Deterministic `@demo` fixture** (`demo_fixture.py`): committed
  template (a static HTML page with visible interaction + a Playwright spec
  tagged `@demo` + a `playwright.config.js` with `video: 'on'`/`trace: 'on'`
  and `webServer: python3 -m http.server` — the page is SERVED locally inside
  the container, not `file://`). The scripted Tester authors it via
  `demo_authoring_script(work_item_id)` — fixture author, real artifacts.
  `DSE_DEMO_BASE_URL` points the same spec at a real preview
  (`TriggerPreview`/`PreviewRef.url` from WS-B/WS-E) when one exists.
- **REAL acceptance** (`tests/test_demo_playwright_in_sandbox.py`):
  `npx playwright test --grep @demo` executed INSIDE a container provisioned
  by the production `docker_driver` (rootless uid 10001, `--read-only`,
  `--cap-drop ALL`, internal network with NO internet,
  `budget={"resource_class": "large", "tmp_mb": 512}`) ends `1 passed` and
  writes a **real `.webm` video** (+ `trace.zip`) into the workspace. Format
  note: Playwright records `.webm` natively (not mp4); transcoding is
  post-processing in the WS-E pipeline if the surface requires it —
  documented in `demo_fixture.py`, aligned with the convention path that WS-E
  consumes (`RunDemoEvidenceInput.demo_dir`, default
  `demos/<work_item_id>/`).
- Additive change in the driver: `ResourceCaps.tmp_mb` (budget key `tmp_mb`,
  64MB default unchanged) — chromium needs more scratch space in `/tmp`.

### WSC-E3-T6 — Second substrate: Claude Agent SDK + conformance (P1)

- **`ClaudeAgentSubstrate`** (`substrate.py`): a real adapter on top of the
  `claude-agent-sdk` PyPI package (**`pip install claude-agent-sdk` worked in
  this session — v0.2.124**; the wheel embeds the CLI). Gateway-only through
  the same triangle as OpenHands: `ANTHROPIC_BASE_URL=<model-gateway>`,
  `ANTHROPIC_API_KEY=<per-task virtual key>`, `ANTHROPIC_CUSTOM_HEADERS=` the
  contract headers (`GatewayCallHeaders`) — never a provider endpoint. The
  toolset is restricted to `Read/Write/Edit/Glob/Grep` (no Bash/git/PR — P1:
  the commit/push stays deterministic in the Activity), `setting_sources=[]`
  (hermetic session, nothing from the host).
- **Switching substrate is PER-DEPLOYMENT CONFIG**
  (`substrate.substrate_from_env`, env `DSE_CODER_SUBSTRATE` in
  `fake|openhands|claude-agent`, default `fake`): the `run_coder_turn`
  Activity builds through the factory; the WS-B workflow calls the Activity by
  name and knows nothing about the substrate — zero workflow code changes to
  switch. An unknown name is a clean `ValueError` (P6), never a silent
  fallback.
- **Conformance suite** (`tests/test_substrate_conformance.py`): the same
  parametrized tests run against `OpenHandsSubstrate` AND
  `ClaudeAgentSubstrate` (both REAL SDKs installed in this venv): the
  `AgentSubstrate` protocol, base_url == gateway and no provider-endpoint
  fragment anywhere in the config, contract policy/budget headers present
  (caps enforced at WS-D call time), a surface with no git/PR/bash, a clean
  error with no session, and env-based selection (including the Activity's
  construction point). This is the NFR-09 compatibility suite — floor pins
  live in `pyproject.toml [project.optional-dependencies].substrates`.
- **Honest limit (same as OpenHands since Phase 1)**: no turn with REAL
  inference is exercised — that requires the model-gateway serving a valid
  virtual key backed by a real provider. Conformance covers
  construction/wiring/selection; the real turn is left to the WS-D
  integration window.

### Phase 3 tests (real, against Docker/Postgres + installed SDKs)

`test_tester_demo_convention.py` (6), `test_demo_playwright_in_sandbox.py`
(1 — the end-to-end acceptance with a real video),
`test_substrate_conformance.py` (16, parametrized) — **23 new tests**. **Real
result of this session: `65 passed` in sandbox-runtime** (15 Phase 1 + 27
Phase 2 + 23 Phase 3) + `13 passed` in egress-proxy + `14 passed` in
packages/contracts (boundary — unchanged, no workflow call site moved) =
**0 failed, 0 skipped**.

## Phase 4 ("Loop hardening & learning") — skill promotion pipeline (WSC-E4-T2/T3)

Closes what Phase 2 deliberately left out: automatic skill curation from
executions. Everything DETERMINISTIC (P1) — no flow decision made by an LLM.
Migrations: `0019_wsc4.sql` (entry gate, already applied — widened status,
`version`, `skill_episode`, `skill_eval`) + `0020_wsc4.sql` (multi-version +
provenance, see below).

### WSC-E4-T2 — Episode capture + candidate materialization (`skill_promotion.py`)

`record_episode(tenant, source, pattern_key, ...)` writes the three *sources
at launch* (§10.17) into `skill_episode`: `clarification` (WS-B), `ci_repair`
and `review_feedback` (WS-E). When a `pattern_key` accumulates
`SUM(occurrence_n) >= threshold` (`DSE_SKILL_CANDIDATE_THRESHOLD`, default 3 —
CONFIG, not an LLM), `materialize_candidates(tenant)` creates a skill with
`status='candidate'`, an incremented `version`, and full `pattern_key` +
`provenance` (which episodes/work items/sources it came from). Idempotent (it
does not re-materialize a skill that already has a live version). A candidate
is NOT served to the Planner and does NOT self-promote — it needs an eval plus
human approval (P3). `created_by='system:skill-promotion'` on purpose: a
candidate is the machine's PROPOSAL; only human approval makes it servable.

### WSC-E4-T3 — Governed promotion pipeline (`eval_skill_candidate`, `promote_skill`)

Two Temporal Activities (names/types from `dse_contracts.activities`, the
Phase 4 entry gate), with the logic in `skill_promotion.py`:

- **`eval_skill_candidate`** → `EvalSkillCandidateResult`: replays the
  candidate against a historical eval set (positives = cases where the skill
  would help; negatives = cases where it must not fire; derived from
  `skill_episode` or injected). `negative_regressions > 0` ⇒ `passed=False`.
  Writes the trail into `skill_eval` (P8).
- **`promote_skill`** → `PromoteSkillResult`: an explicit state machine
  `candidate → approved → canary → active` (+ rollback `active/canary →
  rolled_back`). Invariants **by construction** (they raise BEFORE any write —
  there is no code path that can violate them):
  - **P1/P3**: `to_status in {approved, active}` with no resolved human
    `approver` (empty or `system:*`) ⇒ `ApproverRequired`. Promotion without a
    named human is IMPOSSIBLE — the adversarial test proves it
    (`promote_skill(to_status=active, approver=None)` refuses).
  - `candidate → approved` without a passing eval (`negative_regressions=0`) ⇒
    `EvalGateNotPassed` — a candidate cannot approve itself.
  - a transition outside the machine ⇒ `IllegalTransition`.
  - Every transition → `dse_audit.emit` with the approver's identity.
- **Rollback is a POINTER change inside one transaction** (failure mode 13):
  the served version becomes `rolled_back` and the version it superseded
  (recorded in `provenance.supersedes`) goes back to `active` — in seconds,
  with no reprocessing.

### What the Planner sees (`read_approved_skills`)

The production Planner reads `status IN ('approved','active')`. `candidate`,
`canary`, `draft`, `rolled_back` and `retired` are NEVER served. **`canary` =
shadow in this phase** — there is no canary-subset selection; a canary is
evaluated off the production line and only starts serving once it becomes
`active`. The partial unique index `uq_skill_registry_one_served` (migration
0020) guarantees STRUCTURALLY (not by convention) at most ONE served version
per `(tenant, skill_key)`: the transition that starts serving a new version
demotes the previous one in the same transaction, or the index rejects it —
the Planner never sees two versions of the same skill.

### Migration `0020_wsc4.sql`

Additive. 0010 had `UNIQUE (tenant_id, skill_key)` (ONE row per skill); the
"pointer rollback without losing the previous version" behavior (comment in
0019) requires several versions to coexist as distinct rows. 0020 moves the
uniqueness to `(tenant_id, skill_key, version)`, adds
`pattern_key`/`provenance` and the partial unique "one served version" index.

### Phase 4 tests (real, against Postgres)

- `test_skill_promotion.py` (7): the 3 sources; the threshold (noop below it,
  materializes at it); a candidate is born unserved with provenance;
  idempotency; the eval passes with no regression and fails with a negative
  regression (writing `skill_eval`).
- `test_promotion_pipeline.py` (7) — **the Phase 4 exit criterion**: the full
  candidate→eval→approved→canary→active→rollback flow with the pointer
  returning to the previous version; a rollback with no previous version
  disappears from the Planner; P1/P3 adversarial cases (approver
  `None`/`""`/`system:*` are refused); the eval gate; an illegal transition.
- `test_promotion_activities_wiring.py` (3): both Activities through
  `temporalio.testing.ActivityEnvironment` (real SDK) + the propagated
  refusal.

**17 new tests**; the package suite is **82 passed, 0 failed** in this session.

## What runs on a local fixture/mock (documented, not hidden)

- **`FakeSubstrate`** is the substrate used in EVERY test in this suite — it
  makes no model calls whatsoever, editing files from a Python script supplied
  by the test. This is deliberate: it tests the real plumbing (Docker + git +
  Temporal) without depending on WS-D's model-gateway being up and without
  spending real inference.
- **`model_gateway_client.mint_virtual_key`**: attempts `POST
  {DSE_MODEL_GATEWAY_URL}/internal/virtual-keys` (default
  `http://localhost:4000`, WS-D's reserved port). If the endpoint does not
  answer (WS-D was not yet up when this workstream ran its tests), it falls
  back to a fixture virtual key (`fixture-vk-<work_item_id>-<random>`) — this
  is **clearly signalled** in the `fixture: bool` field of the return value
  and used in the tests. Disable it with
  `DSE_MODEL_GATEWAY_ALLOW_FIXTURE=0` to force a clean failure (P6) instead of
  a silent fixture.
- **The shared-infrastructure Temporal container is down in this session**:
  `docker ps -a` shows `dse_temporal` as `Exited (1)` — the
  `temporalio/auto-setup:1.24` image complains about a missing
  `config/dynamicconfig/development-sql.yaml`. That is `docker-compose.yml`
  (foundation, outside WS-C's editing scope) — I did not try to fix it. That
  is why the Activity tests use `temporalio.testing.ActivityEnvironment` (the
  SDK's official harness, real, not a mock) instead of a Worker connected to a
  real Temporal server — the SDK and the Activity logic are 100% real, there
  is just no workflow/worker running end to end in this session.

## What is missing for production

- **[Phase 1 of plan 09 — PARTIALLY CLOSED in dev]** The Coder turn now runs
  INSIDE the sandbox when `DSE_SANDBOX_INPROCESS=0` (and ALWAYS in
  production): `RemoteSubstrate` dispatches the typed `AgentTurnRequest`
  contract through `SandboxDriver.execute_stage` (`docker exec -i` →
  `agent-runner`; image via `make agent-runner-image`). Live proof in
  `tests/test_isolated_turn_live.py` (the edit arrives only through the bind
  mount; escape denied by the OS). What is STILL missing: (a) the `openhands`
  substrate in the runner (`RemoteWorkspace`/`openhands-agent-server` packaged
  into the image — today it cleanly returns `unsupported_substrate`); (b)
  workspace bootstrap in the K8s driver (clone/checkpoint inside the Pod —
  `execute_stage` via `kubectl exec` exists, but the Pod starts with an empty
  emptyDir); (c) the live proof under gVisor/RuntimeClass in the cluster —
  `pilotReadiness.sandboxIsolationVerified` stays `false` until then.
- **Sandbox image** (`docker/Dockerfile.sandbox-base`): since Phase 3 it is
  genuinely BUILT and EXERCISED (`dse-sandbox-base:wsc3`, 2.35GB — the `@demo`
  acceptance test runs `npx playwright` via `docker exec` inside it), but it
  is still not published to a registry (local build; the tests build it if the
  tag does not exist). The checkpoint/scoped-git scenarios still run their git
  commands against the host path that is the same bind mount as the
  container's workspace (documented in `git_checkpoint.py`); production should
  publish the image to a registry and run git through `docker exec` too.
- **Real integration with `services/model-gateway` (WS-D)**:
  `mint_virtual_key` already tries the real endpoint first; end-to-end
  integration happens in the cross-workstream integration phase, once WS-D is
  actually publishing `/internal/virtual-keys`.
- **`sandbox_leases`/`egress_credential_leases` (migration `0004_wsc.sql`)**
  are additional operational bookkeeping (on top of `audit_log` via
  `dse_audit.emit`, which is the mandatory P8 record) — best-effort, silently
  degrading to "no extra persistence" if Postgres is unreachable (it never
  breaks the main path).

## How to run the tests

```bash
python3.12 -m venv .venv-wsc
source .venv-wsc/bin/activate
pip install -e packages/contracts -e packages/dse_audit -e packages/dse_identity
pip install -e services/sandbox-runtime -e services/egress-proxy
pip install pytest

DSE_DATABASE_URL=postgresql://dse:dse_dev_only@localhost:5432/dse python3 scripts/migrate.py

DSE_DATABASE_URL=postgresql://dse:dse_dev_only@localhost:5432/dse \
  pytest -q services/sandbox-runtime/tests
```

Requires Docker running (real containers are created and destroyed) and the
foundation's Postgres on `localhost:5432` (no Temporal server needed — see the
note above about `ActivityEnvironment`).

**Real result in the Phase 3 session**: `65 passed` in this package (15 Phase 1
+ 27 Phase 2 + 23 Phase 3) / `78 passed` including `services/egress-proxy`
(13), `0 failed`, `0 skipped` (the conditional `OpenHandsSubstrate` and
`ClaudeAgentSubstrate` tests really do run, because `openhands-sdk` and
`claude-agent-sdk` installed successfully in this environment). The `@demo`
acceptance test requires the `dse-sandbox-base:wsc3` image (it builds itself
the first time — minutes; cached afterwards).
