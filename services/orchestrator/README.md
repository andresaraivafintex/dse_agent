# services/orchestrator — WS-B: Temporal orchestration

The Temporal Worker (`temporalio` Python SDK) and the `WorkItemLifecycleWorkflow`
state machine that drives the full lifecycle of a WorkItem in Phase 1 of
Fintex DSE: intake → clarification gate → single Coder → L1 → PR → CI →
human review → human merge → Done, with blocked/failed/escalated as terminal
states and no flow decision ever taken by an LLM (P1) or by an agent session
about its own work (P3).

## Phase 4 ("Loop hardening & learning") — what was added

Phase 4 extends (does not rewrite) the state machine from Phases 1-3. Three
WS-B deliverables, all covered by real tests against the foundation's
Postgres/Temporal (7 new tests in `tests/test_phase4_merge_base_and_learning.py`;
total suite **54 passed**). **No new WS-B migration** — we reuse the
`skill_episode` table from migration `0019_wsc4.sql` (owned by WS-C) for INSERT only. We
only IMPORT from `dse_contracts.activities` (`ACTIVITY_UPDATE_BASE_BRANCH` +
`UpdateBaseBranchResult`, already promoted at the entry gate) — no local
redefinition.

### WSE-E6-T16 — merge-base in the review loop (WS-B wiring)

On the `changes_requested` path of the review loop, BEFORE re-running the Coder, the
workflow calls `ACTIVITY_UPDATE_BASE_BRANCH` (helper
`_update_base_branch_before_review_fix`). Invariants:

- **`first_human_review_done=True` always** at this point: we got here
  precisely because a human already reviewed the PR and requested changes. After the 1st
  review the only viable strategy is merge-base-into-branch — a rebase+force-push
  would orphan the review threads anchored to the rewritten commits
  (verified GitHub behavior, failure mode 11). The strategy choice
  is deterministic and lives in WS-E (P1: code, not a model); WS-B
  only passes the right flag and reacts to the result.
- **`conflict=True` → escalate to a human** (`_EscalateNow`), NEVER force-resolve
  (P6). The escalation carries repo/branch/base so the human can act.
- **`orphaned_threads>0` → escalate** as well: that is the Phase 4 exit assertion
  (zero orphans). If the owner (WS-E) reports >0, the invariant was violated and the
  workflow does not keep going on a guess (P6). Real merge-base guarantees 0 by
  construction; this is caller-side defense in depth.
- The call site is the EXACT payload of `WSB_UPDATE_BASE_PAYLOAD` in
  `packages/contracts/tests/test_activity_boundaries.py` (the file's rule:
  call site + boundary test change together — here the payload already existed and matched,
  so nothing changed in the foundation). The test fakes decode with the REAL model
  `UpdateBaseBranchInput` and return `UpdateBaseBranchResult`.
- **Deliberate scope**: only the `changes_requested` path. That is exactly where there are
  review threads to orphan. On the CI-red path (before any human review)
  there are no anchored threads — the drift there is absorbed by the fix cycle
  itself, and a rebase would even be permissible; that is why merge-base is not forced there.

### WSC-E4-T2 — clarification episode (source of skill-learning input)

The clarification gate already detects the same gap recurring. When a field
that **was already missing in a previous round of this intake** shows up missing again
(detected by pure set arithmetic in the workflow — deterministic, P1), the workflow writes ONE
`skill_episode` (source=`clarification`) into migration 0019's table via the
local Activity `record_skill_episode`:

- `pattern_key = "clarification_missing:<recurring+fields>"` groups
  occurrences of the same pattern; `occurrence_n` is the tenant-wide counter;
  `provenance` (JSONB) carries repo/fields/round/requester.
- **NO skill is created/activated here** (boundary tested in
  `packages/contracts` — WS-B has no promotion activity). The episode is only the
  governable input that WS-C's promotion pipeline consumes. The first gap
  (initial round, before any request) NEVER becomes an episode — only
  recurrence counts (`test_non_recurring_clarification_emits_no_episode`).

### Pilot gate "PR quality thresholds" — PR quality metrics

We emit four OTel metrics through the same mechanism as Phase 3 (`metrics.py` +
the local Activity `emit_pr_quality_metric`, deterministic reads in the workflow /
emission outside the sandbox — P1), at a TERMINAL PR boundary (merge OR
escalation, with the `dse.pr.outcome` attribute):

| Metric | Meaning |
|---|---|
| `dse.pr.review_rounds` | human-review/CI-red rounds per PR (`review_round`) |
| `dse.pr.changes_requested_total` | how many `changes_requested` batches the PR accumulated |
| `dse.pr.time_to_merge_seconds` | from PR finalized (`workflow.now()`, replay-safe) to `merged_by_human` |
| `dse.pr.evidence_refreshes` | evidence refreshes for the PR (proxy for evidence consumption) |

**These four feed the pilot gate "PR quality thresholds" (addendum 03).** The
`time_to_merge` is only emitted on the `merged` outcome. The **authoritative
evidence-consumption** signal (who accessed which artifact, and when) is logged by WS-E in its
access log — WS-B contributes the refresh count as a workflow-side proxy.
Honesty from addendum 03 (administrative blocker): **the real NUMBERS only come out of
operating against real repos/models** — a real GitHub App / Slack / Bedrock account
are prerequisites for the pilot gates and are the longest lead-time item. Here the
INSTRUMENTATION is ready and tested (it emits deterministically and correctly with
the attributes the gate queries); only the real operation to populate the
histograms with pilot data is missing.

## Phase 3 ("Evidence") — what was added

Phase 3 extends (does not rewrite) the state machine from Phases 1+2. Everything
below is covered by real tests against the foundation's Postgres/Temporal (10
new tests; total suite **47 passed**). New migration:
`migrations/0014_wsb3.sql` (table `work_item_evidence`). Contracts: it only
IMPORTS from `dse_contracts.activities` (names + evidence models already
promoted to the foundation at the entry gate) — no local redefinition.

### WSB-E4-T2 — Iteration caps + evidence refresh debounce (ADR-26)

- **No workflow loop is infinite by construction** — every `while` has a tested
  cap: clarification (`clarification_round_cap`, Phase 1), L1 fix
  (`coder_retry_cap`, Phase 1), L2 objections (`l2_retry_cap`, Phase 2), re_plan
  (`plan_round_cap`, Phase 2) and — new — **review rounds**
  (`review_round_cap`, default 20; exhausted → `escalated`, tested in
  `test_iteration_caps_debounce.py`). All caps come from the workflow INPUT,
  filled in by `config.from_env()` (`DSE_REVIEW_ROUND_CAP` etc.) — they change
  without a redeploy; per-tenant is possible by reading `tenant_config` before
  `apply_to_input` in the dispatcher.
- **Evidence debounce (ADR-26), 100% deterministic (P1)**: evidence is
  regenerated **only** when (a) a fix cycle ran (= a new commit that
  changes behavior) or (b) a human explicitly asked (the new
  `@workflow.signal refresh_evidence`). Review comments **accumulate in a
  list** and are consumed in BATCH: if the batch requests changes and a window is
  configured (`evidence_debounce_seconds`, prod default 300s; 0 = no
  window), the workflow waits out the window on a durable timer to group
  comments still arriving → **6 comments in one window = 1 fix cycle +
  1 refresh** (proven with time-skipping in
  `test_six_review_comments_in_window_trigger_at_most_one_refresh`).
- **Refresh cap** (`evidence_refresh_cap`, default 5, beyond the initial one):
  exceeded → declines CLEANLY and audited (`evidence_refresh_declined_cap`, P6);
  the evidence goes stale but the PR is never blocked.

### Evidence pipeline wiring (after `finalize_pr`)

```
finalize_pr -> trigger_preview(files_changed from CoderTurnResult, FR-20)
   ├─ skipped_backend_only -> record it and move on (counts as success)
   ├─ degraded / Activity crashed -> evidence_degraded (failure mode 9) and MOVE ON
   └─ created -> run_demo_evidence(preview base_url; INTERNAL publish, WS-E)
                   -> run_visual_diff (base_screenshot_key=None on the 1st run -> baseline)
```

- Names/models imported from `dse_contracts` (`ACTIVITY_TRIGGER_PREVIEW`/
  `RUN_DEMO_EVIDENCE`/`RUN_VISUAL_DIFF`, `PreviewRef`/`DemoEvidenceResult`/
  `VisualDiffResult`). **A preview failure does NOT block the PR** (failure mode
  9): any degradation becomes an `evidence_degraded` audit row + projection and the flow
  continues to human review — proven in `test_evidence_pipeline.py`
  (degraded, and a total crash of the Activity).
- The tests use fakes **typed by the contract's REAL models**: each fake
  decodes the payload with `TriggerPreviewInput(**payload)` etc. — a payload that
  drifts from the contract breaks in the test, not on the wire (lesson from addendum 02).
  The call sites' exact payloads are also in
  `packages/contracts/tests/test_activity_boundaries.py` (the only edit made
  under `packages/`, permitted by that file's rule: call site and boundary test
  change together).
- Durable, queryable projection: table `work_item_evidence` (migration 0014,
  idempotent upsert via the local Activity `record_evidence_state`) — the queue
  board (WS-F) can read "what is the latest preview/video for this PR?" without scanning
  the audit ledger (which remains the immutable source, P8).

### Activating the history alert (with WS-F — ALERTING-RULES.md §3)

- The workflow reads `workflow.info().get_current_history_length()` /
  `get_current_history_size()` (replay-safe SDK APIs) and the Continue-As-New
  count (`continue_as_new_count`, incremented on EVERY `_continue_as_new`
  transition) and emits, via the local Activity `emit_history_metric`
  (I/O outside the sandbox — P1), the OTel metrics:
  - `dse.workflow.history_length` (histogram, `{event}`)
  - `dse.workflow.history_size_bytes` (histogram, `By`)
  - `dse.workflow.continue_as_new_count` (histogram, `{run}`)
  with the attributes `dse.work_item_id`, `dse.tenant_id`, `dse.stage`,
  `dse.checkpoint`. Emitted: before each `continue_as_new`, after
  `pr_finalized`, and on **every pass through the review loop** (exactly where history
  grows without a Continue-As-New — the documented Phase 1 limitation that the
  debounce mitigates). Best-effort: a metric failure never affects the flow.
- Exporter: same env as tracing — `DSE_OTEL_EXPORTER=console` (default) or
  `otlp` + `DSE_OTEL_EXPORTER_OTLP_ENDPOINT` (the `docker-compose.wsb.yml`
  fragment already points at WS-F's `otel-collector:4317`). **WS-F
  points rule §3 (Warning 70% / Critical 90% of ~10k events) at
  `dse.workflow.history_length`.** Tested with the OTel SDK's real
  `InMemoryMetricReader` in `test_history_metric.py`.

### Contract field requests (documented, I did NOT edit the foundation)

1. **`DemoEvidenceResult` carries no screenshot key/path** — the
   "run_visual_diff when there is a screenshot" trigger is today approximated
   deterministically by "the demo produced media (video/trace)" + the
   `demos/<work_item_id>/screenshot.png` convention (ADR-27). Request: a
   `screenshot_artifact_key: str | None` (or `screenshot_path`) field on
   `DemoEvidenceResult`.
2. **`VisualDiffResult` does not return the created baseline's key** — when
   `baseline_created=True`, I assume WS-E returns the baseline key in
   `diff_artifact_key` (stored in `visual_baseline_key` for the next run).
   Request: a `baseline_artifact_key: str | None` field.

### New signals/fields (Phase 3)

- `@workflow.signal refresh_evidence(payload=None)` — an explicit human request
  for a refresh (routable by WS-A from a dedicated comment/button).
- `WorkItemLifecycleInput`: `review_round_cap`, `evidence_debounce_seconds`,
  `evidence_refresh_cap`, `evidence_refreshes`, `preview_status/url`,
  `evidence_passed/video_key/trace_key`, `visual_baseline_key`,
  `last_files_changed` (a human refresh has no new commit — it reuses the last
  set for the FR-20 paths-filter), `continue_as_new_count`.

## Phase 2 ("Judgment & queue") — what was added

Phase 2 extends (does not rewrite) the Phase 1 state machine. Everything below is
covered by real automated tests against the foundation's Postgres/Temporal
(20 new tests; total suite **37 passed**). New migration:
`migrations/0009_wsb2.sql` (table `plan_approval_gate`).

### New session sequence (WSB-E2-T3, extended)

The workflow now orchestrates, within the implementation phase:

```
[budget at admission] -> Planner (read-only) -> [plan approval GATE]
  -> provision -> ( Coder -> Tester -> L1 )*  -> L2 (fresh context) -> PR
```

- Activity names come from `dse_contracts`: `ACTIVITY_RUN_PLANNER_TURN`,
  `ACTIVITY_RUN_TESTER_TURN`, `ACTIVITY_RUN_L2_REVIEW` (defensive import — the
  real ones come from WS-C/WS-E; tests use fakes with the same signature in
  `tests/fakes.py`).
- **P3 (non-negotiable) at L2**: `_run_l2_review` builds the payload with
  **exactly** `plan` + `diff_summary` + `files_changed` — never
  `instructions`/`clarification_notes`/`objections`/the Coder's history.
  Proven in `test_phase2_sequence.py::test_l2_review_receives_only_plan_and_diff_not_coder_history`.
- L2 objections go back to the Coder (capped by `l2_retry_cap`); exhausted ->
  escalate. A PR is never finalized with an open objection (P6).

### Plan approval gate by risk class (WSB-E3-T2)

- New `@workflow.signal` **`plan_approval`** (name = `SIGNAL_PLAN_APPROVAL`).
  Payload: `{verdict: approved|rejected, route: re_plan|re_clarify|cancel,
  comment/justification, actor}`.
- **Policy lives outside the model (P1)**: `dse_orchestrator/policy.py`.
  `classify_risk()` performs **deterministic defense-in-depth
  classification** — a plan touching `migrations/`, `.github/workflows/`,
  `auth/`, `billing/`… is `high` **even if the Planner declares `low`** (a
  model under-classifying does not lower the gate). `requires_plan_approval()`
  consults the `require_approval_risk_classes` set (operator config,
  default `{high}`), never the model.
- `low` -> **auto-approved by policy** (never by absence of an approver).
  `high` -> parks in the durable state **`awaiting_plan_approval`**, resolves the
  approver through the **CODEOWNERS -> designated approvers of the access bundle
  (WS-F `dse_access_bundle`) cascade**, renders the request through the adapters
  (`post_tracking_comment`), and waits durably for `plan_approval`.
- **An EMPTY cascade = Blocked + escalation, NEVER auto-approve by absence**
  (`_finish_blocked` + audit `plan_gate_no_approver_blocked`). Offboarded approvers
  (`dse_console_identity.active=false`) are filtered out.
- Durable projection queryable by the queue board (WS-F): table
  `plan_approval_gate` (idempotent upsert per work item; it does **not** replace the
  audit ledger — P8: `audit_log` remains the immutable source).

### Rejection path (WSB-E3-T3)

3 deterministic routes, always audited with **identity + justification**,
and none of them starts implementation without passing through the corresponding gate again:
- `re_plan` -> re-runs the Planner + gate (capped by `plan_round_cap`);
- `re_clarify` -> `continue_as_new` back to the **clarification gate** (reopens the
  round by clearing `acceptance_criteria`);
- `cancel` -> terminal Failed.

### Budgets at admission and at boundaries (WSB-E4-T1)

- `budget_max_usd` is read from the `work_items.budget` JSONB (key `max_usd`) at
  admission; `spent_usd` **aggregates the `cost_usd` reported by the gateway (WS-D)**
  in every model Activity result (coder/tester/l2).
- Checked at admission and at **every phase boundary** (`_budget_boundary`) —
  **it never cuts in the middle of an Activity (P6)**. Exhausted -> Failed with a
  clear message. The operator can raise it via `@workflow.signal raise_budget` (applied at
  the next boundary, resuming without restarting). Every budget event -> audit.

### Per-tenant fairness, worker-side (WSB-E1-T3)

- `dse_orchestrator/fairness.py`: a **swappable interface** `FairnessController`.
  `WorkerSideFairnessController` enforces a **per-tenant Activity concurrency
  cap** (a per-tenant semaphore) read from `tenant_config`
  (`fairness->>'max_concurrent_activities'` > `max_concurrent_work_items`), through
  an inbound Activity `FairnessInterceptor`. Once the server supports native P&F
  (1.31+, unavailable — we are on 1.29), it is swapped for
  `NativeFairnessController` (a no-op in the worker; it delegates to the server via
  `fairness_key`) **without touching the workflow** — only the Worker assembly
  (`--fairness-mode`).
- **Burst test** (`test_fairness.py`): one tenant saturating its cap does not
  push the other's dispatch beyond the SLO (real concurrency, wall clock).
  The interceptor is validated against a real Temporal Worker (peak ≤ cap).

### Model-path chaos + fail-closed proxy (WSB-E5-T3b)

`tests/test_chaos.py` extended (beyond Phase 1's worker kill):
- egress-proxy unavailable / expired virtual key / kill switch -> the model
  Activity refuses **non-retryable**; the orchestrator **fails cleanly at the
  boundary, with no truncated output (P6)**, audited — via `_run_model_activity`,
  which converts the fail-closed policy refusal into `_FailClosed`;
- LiteLLM **flapping** (transient/retryable) -> Temporal's durability
  re-runs the Activity and the task **completes without losing progress** (1 PR).

### `awaiting_plan_approval` state — documented enum gap

The `awaiting_plan_approval` status value is written to the TEXT column
`work_items.status` (no CHECK; `dse_contracts.constants` itself already references
that string as the routing trigger for `SIGNAL_PLAN_APPROVAL` in WS-A).
However, the **`dse_contracts.work_item.WorkItemStatus` enum (foundation, not editable
by this workstream) still has no such member**, nor does the public map
`to_public_status` project it (it should -> `"blocked"`). The orchestrator works around this
with `STATUS_AWAITING_PLAN_APPROVAL` + `_set_raw_status` (see `workflows.py`).
**Recommended foundation action**: add `awaiting_plan_approval` to the enum and
to `_PUBLIC_STATUS_MAP`.

### Honest boundary gaps (to reconcile at integration)

- The **CODEOWNERS reader** (`policy.set_codeowners_reader`) is an injection point:
  production = the GitHub adapter (WS-A) reading the file through the GitHub App; the local
  default returns `None` (no GitHub). Tests inject a fake. The cascade falls through to the
  access bundle (WS-F) when CODEOWNERS is empty.
- The **Planner's cost** does not count toward `spent_usd` (`PlanArtifact` does not carry
  `cost_usd`); coder/tester/l2 do. To reconcile if WS-C starts reporting
  the planner's cost.
- **Real model chaos** (actual LiteLLM/virtual key/egress-proxy) is
  simulated at the Activity boundary with the **same error type**
  (`ApplicationError non_retryable` vs retryable) that WS-D marks — the real
  end-to-end integration belongs to WS-D/WS-C's suite.

## Status — what is implemented and working

All P0 tasks from the brief (WSB-E1, E2, E3-T1/T4, E5) are
implemented and covered by automated tests running against the **real**
Postgres and Temporal of the foundation infrastructure (never mocked):

- **WSB-E1-T1/T2/T4** — `worker.py`: connects to `localhost:7233` (or
  `DSE_TEMPORAL_ADDRESS`), task queue = `dse_contracts.constants.TASK_QUEUE`,
  fixed build id (`--build-id`/`DSE_WORKER_BUILD_ID`), an HTTP health endpoint on
  `:8900` (`GET /health`), a real OpenTelemetry interceptor
  (`temporalio.contrib.opentelemetry.TracingInterceptor`, configured in
  `otel_interceptor.py`) and the `emit_audit_event` Activity
  (`dse_contracts.activities.ACTIVITY_EMIT_AUDIT`) that writes to the audit ledger
  via `dse_audit.emit` from inside an Activity. Worker Versioning /
  drain-and-cutover runbook in `RUNBOOK.md`.
- **WSB-E2** (all 4 tasks) — `workflows.py`:
  `WorkItemLifecycleWorkflow` (`@workflow.defn(name=WORKFLOW_TYPE)`)
  implements the complete state machine using
  `dse_contracts.work_item.WorkItemStatus`; all I/O lives in an Activity (never
  directly in the workflow body — see the "Determinism discipline" section
  below); `continue_as_new` closes out the intake phase; `start_workflow` is
  idempotent by `workflow_id=work_item_id` (Temporal natively rejects with
  `WorkflowAlreadyStartedError` — no extra implementation was needed);
  cross-workstream Activities are sequenced BY NAME
  (`workflow.execute_activity(ACTIVITY_..., ...)`); steering signals
  (`clarification_answer`, `review_comment`, `approval_or_rejection` — the
  latter implemented as the `review_comment`+`merged_by_human` pair, see the
  note below) are correlated externally by WS-A.
- **WSB-E3-T1** — clarification gate: a deterministic checklist via an
  Activity (`check_clarification_completeness` — repo/base_branch/
  acceptance_criteria), a configurable reminder timer + escalation,
  capped rounds (default 3), never "guesses" — once the cap or the
  reminder+escalation window elapses with no answer, it transitions to `escalated`.
- **WSB-E3-T4** — human review loop: a durable wait for
  `review_comment` (`changes_requested`/`approved`); `changes_requested`
  goes back to the Coder on the SAME branch/PR, re-validates L1, re-finalizes the SAME PR;
  `approved` waits for a SECOND signal (`merged_by_human`) and only then
  transitions to Done. **There is no call to GitHub's merge API
  anywhere in the code** — proven statically in
  `tests/test_review_loop.py::test_no_automatic_merge_path_in_source`
  (grepping for real call patterns, not for prose in a comment).
- **WSB-E5** (all 3 tasks):
  - T1: a checkpoint at the end of each phase (`ACTIVITY_CHECKPOINT_SANDBOX`) with
    bounded retries; exhausted, it attempts a rebuild (`ACTIVITY_REBUILD_SANDBOX`);
    exhausted too, it escalates.
  - T2: operator signals/queries — `pause`/`resume`, `cancel` (+teardown
    via Activity), `retry_from_checkpoint`, `force_clarification`,
    `escalate`, `reassign_model`, `reassign_runtime`; the kill switch is checked
    before EVERY business Activity (it never kills an Activity already in
    flight); every operator action causing a transition calls
    `ACTIVITY_EMIT_AUDIT`.
  - **T3 (critical) — chaos suite** (`tests/test_chaos.py`): it genuinely kills
    a Temporal worker **process** (SIGKILL) in the middle of a long
    Activity, brings up a second worker, and proves by querying the real
    Postgres/audit ledger that the workflow resumed without losing progress
    and without duplicating business effects (`pr_finalized`, `merged_by_human`,
    `sandbox_provisioned` each appear exactly once, even though the low-level
    Activity was re-executed — correct at-least-once behavior).

### Note on signal names

The brief lists `approval_or_rejection` as one of the steering signals.
I implemented the review verdict as `review_comment` (payload with
`verdict: "changes_requested" | "approved"`) followed, in the approved case, by
a second dedicated signal `merged_by_human` — because "approved" and "merged"
are in fact two distinct events correlated to different GitHub webhooks
(`pull_request_review` vs. `pull_request.closed+merged=true`), and
merging them into a single signal would hide exactly the point of P3 (approval
≠ merge). `clarification_answer` is implemented literally as
requested.

## What runs on a local fixture/fake (by design for this task)

The cross-workstream Activities from WS-C (`sandbox_runtime.activities`) and
WS-E (`validation.activities`) **do not exist yet** (they are being built in
parallel). All the tests use **FAKE Activities** implementing the SAME
signature/name/return type as `dse_contracts.activities`
(`tests/fakes.py`) — we never mock Postgres or Temporal themselves, only those
two boundaries that belong to another workstream. `worker.py` tries to
import the real modules defensively (`try/except ImportError`, see the
next section) and falls back to running with only WS-B's local Activities
if they do not exist yet.

## Assumed Activity contract for the WS-C/WS-E boundaries

`dse_contracts.activities` defines **names** and **return types**, but not
each Activity's input payload schema (that is deliberately
left open to parallelize development). I assumed the following
payload contract (a single `dict` per call) — **to be reconciled at
integration** with whoever implements them for real:

| Activity | assumed payload (dict) | returns |
|---|---|---|
| `provision_sandbox` | `work_item_id, tenant_id, repo, base_branch` | `SandboxHandle` |
| `run_coder_turn` | `sandbox_id, work_item_id, tenant_id, instructions: list[str], model_override, runtime_override` | `CoderTurnResult` |
| `checkpoint_sandbox` | `sandbox_id, work_item_id, phase` | `CheckpointRef` |
| `rebuild_sandbox` | `work_item_id, sandbox_id` | `SandboxHandle` |
| `teardown_sandbox` | `sandbox_id, work_item_id, reason` | `None` |
| `run_l1_pipeline` | `work_item_id, sandbox_id` | `L1Result` |
| `finalize_pr` | `work_item_id, tenant_id, sandbox_id, repo, base_branch, branch, existing_pr_number?` | `PrRef` |
| `post_tracking_comment` | `work_item_id, tenant_id, pr_number, status` | `None` |
| `consume_ci_status` | `work_item_id, pr_number` | `CiStatusResult` |

When WS-C/WS-E are done, they should register an `ACTIVITIES` list (a list of
callables decorated with `@activity.defn(name=...)`) in `services/sandbox-runtime/activities.py`
and `services/validation/activities.py` — `worker.py` imports
those two modules automatically (`_load_cross_workstream_activities`) and
registers everything it finds, with no need to edit `worker.py` again. If the
real payload diverges from the one assumed above, it is just a matter of adjusting the
`dict`s assembled in `workflows.py` (all centralized, easy to find by grepping for
`ACTIVITY_`).

## Real integration finding: the `start_workflow` contract

While running the worker against the shared infrastructure's real Temporal (the same
`dse-core-task-queue` WS-A also uses), I discovered that WS-A's dispatcher
appears to call `StartWorkflow` passing **only the `work_item_id`
(string)** as the argument, not a complete `WorkItemLifecycleInput`. So as
not to block the integration on that, `WorkItemLifecycleWorkflow.run` now accepts
`Any` and performs a defensive coercion (`_coerce_input`, in `workflows.py`):

- `WorkItemLifecycleInput` → used directly (this is always what the internal
  `continue_as_new` calls pass).
- `dict` → builds `WorkItemLifecycleInput(**dict)`.
- `str` → treated as `work_item_id`, with the rest (`tenant_id`, `repo`,
  `base_branch`, `requester`, `pr_number`) fetched from the `work_items` table via a
  new local Activity, `load_work_item`.

This means WS-A can keep calling
`start_workflow(workflow_id=work_item_id, args=[work_item_id])` (or with the
full object, if they prefer to align) without breaking. **To reconcile**: if
the WS-A team confirms which format is definitive, we can simplify by
removing the unused branch.

## What needs real credentials/infrastructure for production

- **Real Coder/L1/PR/CI**: they depend on the WS-C/WS-E Activities (Docker
  sandbox, OpenHands, egress proxy, LiteLLM, GitHub App, SAST/secret-scan).
  None of that is implemented here — only the orchestration that invokes them by
  name.
- **OpenTelemetry**: by default it exports to the console
  (`DSE_OTEL_EXPORTER=console`, local/dev mode). For production, set
  `DSE_OTEL_EXPORTER=otlp` + `DSE_OTEL_EXPORTER_OTLP_ENDPOINT=<host:port>`
  of a real collector (WS-F) and install
  `opentelemetry-exporter-otlp-proto-grpc` (not a required dependency of
  this package — it falls back to the console with a clear warning if absent).
- **Worker Versioning**: off by default (`DSE_WORKER_USE_VERSIONING=false`)
  — see `RUNBOOK.md` for how and when to enable it in production.

## Determinism discipline (P1) and the "clobber bug" this code avoids

During development, the tests caught a real signal race:
resetting a "signal received" flag **before** waiting on it (a common
pattern, `self._x_received = False; await wait_condition(...)`) can erase
a signal that already arrived while the workflow was still processing a
previous Activity in the same loop — because the signal *handler* runs as soon
as the workflow's event loop yields control (at every `await`), not only when
we reach the `wait_condition`. `workflows.py` documents this inline in every
place where it matters (`_run_intake_phase`, `_run_review_phase`) and the rule
adopted is: **never reset a signal flag before consuming it; always reset
AFTER reading the payload**.

A second finding, documented in `_run_implementation_phase`: calling
`continue_as_new` immediately before a phase that starts by waiting on an
external signal (e.g. `review_comment`) is a real race — a signal
addressed to the "old" run that is closing down can be lost, never
delivered to the new run. That is why `continue_as_new` is only used for the
intake→implementation transition (demonstrably safe: nothing relevant can arrive
before the PR exists); implementation→review and the entire review loop
run in the SAME execution (a single `while True`), trading a bit of history
reset for test-proven correctness. This is
documented inline in the code (`# We do NOT continue_as_new here`) and is the
reason `test_chaos.py` also serves as a regression test for that class of
bug (history replay of a longer execution).

## Files

```
services/orchestrator/
  pyproject.toml
  Dockerfile
  RUNBOOK.md
  README.md
  src/dse_orchestrator/
    __init__.py
    config.py            # OrchestratorConfig + from_env() + apply_to_input()
    models.py             # WorkItemLifecycleInput/Result, phases, OperatorEvent
    local_activities.py   # update_work_item_status, check_clarification_completeness,
                           # emit_audit_event (ACTIVITY_EMIT_AUDIT), load_work_item
    otel_interceptor.py   # setup_tracing() -> real TracingInterceptor
    metrics.py             # Phase 3: OTel metric dse.workflow.history_length (§3)
    workflows.py           # WorkItemLifecycleWorkflow (the state machine)
    worker.py              # entrypoint: Client.connect + Worker + health endpoint
  tests/
    conftest.py            # real-Postgres helpers + time_skipping_env fixture
    fakes.py                # FAKE Activities (same signature as dse_contracts.activities;
                            # Phase 3: evidence fakes decode with the REAL models)
    test_lifecycle_happy_path.py
    test_clarification_gate.py
    test_review_loop.py
    test_operator_controls.py
    test_chaos.py           # WSB-E5-T3 (real chaos, worker process killed)
    chaos_worker_process.py # subprocess used by the chaos test
    test_evidence_pipeline.py       # Phase 3: preview/demo/visual diff wiring + failure mode 9
    test_iteration_caps_debounce.py # Phase 3: WSB-E4-T2 (caps + ADR-26 debounce)
    test_history_metric.py          # Phase 3: history metric with InMemoryMetricReader
../../docker-compose.wsb.yml  # WS-B's reserved fragment (port 8900)
../../migrations/0003_wsb.sql # NOT CREATED — see the note below
```

### About `migrations/0003_wsb.sql`

It was not created. Temporal persists its own workflow/signal/timer state
in the foundation's Postgres (schema managed by Temporal's own
`auto-setup`, outside our control). The only shared table the
orchestrator writes to is `work_items` (columns `status`/`pr_number`), already
created by the foundation's `0001_foundation.sql` migration — there is no need
for a WS-B-owned table in this Phase 1.

## Connection to WS-A's chaos test (NFR-01, the two halves)

`test_chaos.py` kills the **worker** that executes workflows/activities after
they have already started. WS-A's equivalent chaos test (WSA-E1-T3) kills the
**dispatcher** — the process that runs `SELECT ... FOR UPDATE SKIP LOCKED` on the
`ingest_events`/`work_items` table and calls `StartWorkflow`. Together, the two
tests cover both halves of NFR-01 (end-to-end durability):

- WSA-E1-T3 proves that no event gets stuck or processed twice
  **before** the workflow exists (the intake/outbox phase).
  `SELECT...FOR UPDATE SKIP LOCKED` guarantees that if the dispatcher dies
  after taking the lock but before confirming the `StartWorkflow`, the row
  becomes available for another process to pick up (the lock is released on the
  transaction rollback); `start_workflow(workflow_id=work_item_id)` being idempotent
  guarantees that if it dies AFTER the StartWorkflow but before marking the
  event `processed`, reprocessing the same event does not duplicate the
  workflow (Temporal rejects it with `WorkflowAlreadyStartedError`).
- WSB-E5-T3 (this file) proves that no progress is lost or
  duplicated **after** the workflow exists and is in flight, even if the
  worker running it dies in the middle of an Activity.

## How to run the tests

```bash
cd /Users/saraiva/Documents/DSE/fase1
python3.12 -m venv .venv-wsb
source .venv-wsb/bin/activate
pip install -e packages/contracts -e packages/dse_audit -e packages/dse_identity
pip install -e services/orchestrator
pip install pytest pytest-asyncio

cd services/orchestrator
pytest -q
```

Prerequisite: the foundation infrastructure already up (`docker compose up -d` was
run before this session — Postgres on `localhost:5432`, Temporal on
`localhost:7233`). The tests use:
- `temporalio.testing.WorkflowEnvironment.start_time_skipping()` (a
  real, ephemeral test Temporal server with time acceleration) for
  most tests — it accelerates the reminder/escalation timers from
  hours/days down to seconds with no real sleep.
- The infrastructure's **real** Temporal (`localhost:7233`) for `test_chaos.py`
  specifically — because killing a worker process for real only proves
  something against a real server.
- The infrastructure's **real** Postgres for every audit/status write
  (`dse_audit.emit`, `update_work_item_status`) in all tests.

`_require_postgres` (an autouse fixture in `conftest.py`) skips the tests with
a clear message if the foundation's Postgres is unreachable, rather
than failing with a cryptic connection error.

## Actual suite result (run in this session)

```
17 passed in ~23s
```

Coverage: 3 happy-path lifecycle tests + L1 retry (2 variations),
4 clarification gate tests (complete, reminder, escalation on
silence, round cap), 4 review loop tests (changes_requested,
CI red, waiting for explicit merge, static anti-automatic-merge grep),
4 operator control tests (pause/resume, cancel+teardown, reassign,
escalate), 1 critical chaos test + 1 sanity check of the helper script.

## What is incomplete / known limitations

1. **The cross-workstream Activity payload is a documented assumption**
   (see the table above) — it will only be truly validated when WS-C/WS-E
   publish their real Activities and the worker imports them via
   `_load_cross_workstream_activities`.
2. **`continue_as_new` is not used at every phase boundary** listed
   in the brief (only intake→implementation) — a deliberate decision after finding
   a real signal race in a test (see the "Determinism
   discipline" section above). The history of an execution with many
   `changes_requested` cycles grows more than ideal; for the volume expected
   in Phase 1 (few review cycles per PR) that is acceptable, but it is
   recorded as a limitation to revisit if Temporal complains about
   history size in production (it warns via
   `workflow_task_timeout`/size warnings before turning into an error).
3. **Residual race on operator signals at the intake→implementation boundary**:
   since only that transition uses `continue_as_new`, an operator signal
   (`pause`/`cancel`/etc.) sent at the exact instant of that transition
   could theoretically be lost, for the same reason as item 2. Mitigation:
   the operator can resend the signal (idempotent from a business
   standpoint); all the OTHER boundaries (within implementation and review,
   which is now most of the workflow's lifetime) do not carry that risk.
4. ~~No fairness/budget enforcement~~ — **implemented in Phase 2** (WSB-E1-T3
   worker-side fairness + WSB-E4 budgets). See the "Phase 2" section at the top.
5. ~~No Planner/Tester/Reviewer split and no plan approval gate~~ —
   **implemented in Phase 2** (WSB-E2-T3 extended + WSB-E3-T2/T3). See the
   "Phase 2" section at the top.
6. **`worker.py` runs without TLS/mTLS to Temporal** (a plain `Client.connect`)
   — fine for the local dev Temporal; production would need
   `tls=...`/a real namespace, outside the scope of this local session.
