# services/validation — WS-E (L1/L2/L3 validation + PR finalizer + evidence)

Fintex DSE. Implements the `services/validation/` component described in `CONVENTIONS.md`:
the L1 pipeline (lint/typecheck/test/build + SAST/secret-scan + diff-budget/
forbidden-paths), an idempotent PR finalizer, minimal CI status consumption, and
the resume-on-review-comment handler (UC4). **Phase 2 ("Judgment & queue")**
adds: orchestration of the fresh-context L2 loop (WSE-E2-T4), the bounded
L2->Coder fix-retry loop (WSE-E2-T5), and **strict PR mode**, in which a
human opens the PR (WSE-E3-T8, unblocked by the new `PrRef.compare_url`).

> **Phase 4 — summary of what was added** (details below in §Phase 4):
> merge-base against REAL git (`merge_base.py` — never rebase during human
> review, zero orphaned threads, failure mode 11) exposed as the contract
> Activity `update_base_branch` (`ACTIVITY_UPDATE_BASE_BRANCH`); emission of
> skill-learning episodes for accepted review feedback (`review_learning.py`,
> `skill_episode` source=`review_feedback` from migration 0019 — NO skill is
> created/activated, boundary is tested); migration `migrations/0020_wse4.sql`
> (`wse_base_updates`). **114 tests passing** (45 P1 + 26 P2 + 32 P3 + 11 P4),
> against real git/Postgres/Temporal/Garage/k3d+Argo CD/Playwright — nothing mocked.

> **Phase 3 — summary of what was added** (details below in §Phase 3):
> real Garage artifact store (`evidence/garage.py` + `docker-compose.wse.yml`),
> real Playwright @demo video (`evidence/demo.py` + pinned runner in
> `playwright/`), per-PR previews via Argo CD ApplicationSet against the real
> k3d cluster (`preview/` + our own git smart HTTP in `gitserver/`), full L3
> (`github/l3.py`), Pillow visual diff (`evidence/visual_diff.py`), consolidated/
> debounced publication (`evidence/publication.py`), migration
> `migrations/0017_wse3.sql`. All 4 Phase 3 CONTRACT Activities
> (`publish_artifact`, `run_demo_evidence`, `trigger_preview`,
> `run_visual_diff`) are registered in `ALL_ACTIVITIES`. **103 tests passing**
> (45 P1 + 26 P2 + 32 P3), against real Garage/Postgres/Temporal/k3d+Argo CD/
> Playwright — nothing mocked where durability/policy is concerned.

> **Phase 2 — summary of what was added** (details below in §Phase 2):
> `dse_validation/l2/` (session/l2_review/fix_loop), 3 new Activities
> (`wse_run_l2_review`, `wse_record_fix_loop`, `wse_adopt_pr`), `strict_mode`
> wired into `finalize_pr_core`, `migrations/0012_wse2.sql`
> (`wse_l2_reviews`, `wse_fix_loops`, nullable `pr_number` + `compare_url` in
> `wse_pr_tracking`). **71 tests passing** (45 Phase 1 + 26 Phase 2), against real
> Postgres, nothing mocked where durability/idempotency is concerned.

Reuses (does not reimplement) the foundation contracts: `dse_contracts.mutable_comment.MutableCommentWriter`,
`dse_contracts.plan_artifact.PlanArtifact`, `dse_contracts.activities.{SandboxHandle,L1Finding,L1Result,PrRef,CiStatusResult}`,
the names `ACTIVITY_RUN_L1_PIPELINE` / `ACTIVITY_FINALIZE_PR` / `ACTIVITY_CONSUME_CI_STATUS`,
`dse_audit.emit`, `dse_identity.resolve_principal`.

## What is implemented and working (real, tested against real infrastructure)

### WSE-E1 — L1: deterministic in-sandbox gates

- `dse_validation/l1/quality_checks.py` — **T1**: `lint_check`, `typecheck_check`,
  `test_check`, `build_check`. They run through `SandboxExecutor` (see below),
  parse STRUCTURED output (lint issue counts, type errors, pytest summary —
  not just the exit code) and never truncate output (P6): a timeout becomes an
  explicit `L1Finding(passed=False)`, and so does a missing command.
- `dse_validation/l1/sast.py` — **T2 (SAST)**: actually runs `bandit -r <dir> -f json`
  and normalizes the JSON into an `L1Finding`, gated by severity
  (`DSE_L1_SAST_SEVERITY_GATE`, default `MEDIUM`).
- `dse_validation/l1/secret_scan.py` — **T2 (secret-scan)**: our own scanner
  (regex + Shannon entropy, pure stdlib, no external dependency) that
  runs INSIDE the sandbox via `python3 -c <script>`: AWS access key id,
  GitHub/Slack tokens, PEM private key header, and the generic case
  "variable with a secret-sounding name == high-entropy literal" (with a list of
  obvious placeholders ignored: `changeme`, `xxx`, etc).
- `dse_validation/l1/plan_compliance.py` — **T3**: a real `git diff --numstat
  <base_branch>...HEAD` inside the sandbox, compared against
  `PlanArtifact.expected_files`, `diff_budget_lines` and `forbidden_paths`.
  Produces exactly 2 findings (`diff_budget`, `forbidden_paths`), each one
  naming the violated plan field in its message when it fails.
- `dse_validation/l1/pipeline.py` — joins the 8 checks into a single `L1Result`
  (`work_item_id`, `passed`, `findings`), persists it to `validation_runs`
  (Postgres) and emits 1 audit row (`l1_pipeline_run`, P8). A failing check does
  not stop the others from running (it never bails out halfway) and it decides
  nothing on its own — deciding "back to the Coder" is the WS-B workflow's job.

### WSE-E3 — Deterministic PR finalizer

- `dse_validation/github/pr_finalizer.py` — **T6**: `finalize_pr_core` pushes
  the branch (via `SandboxExecutor` + `git push`, GitHub App credentials),
  and opens EXACTLY 1 PR per `work_item_id` from a fixed template
  (title with `work_item_id` + summary; body with WorkItem ID, risk_class,
  test evidence link, back-link `Closes #<issue>`). **P1**: no part of this
  uses an LLM — title/body are a fixed Python template.
  **Idempotency** is proven in `tests/test_pr_finalizer_idempotent.py`
  (3 real scenarios against Postgres: first creation, re-run after
  success, and a re-run simulating "the process died between creating the PR via
  the API and persisting `pr_number`" — none of the 3 ever creates a second PR).
- `dse_validation/github/comment_backend.py` — **T7**: `GitHubCommentBackend`
  implements the foundation's `CommentBackend` Protocol; used with the
  already-built `MutableCommentWriter` (not reimplemented) + `PostgresCommentStateStore`
  (table `wse_comment_refs`, real Postgres) for the PR's single tracking comment.
- **T8 — "strict mode"**: **IMPLEMENTED in Phase 2** (the contract gained
  `PrRef.compare_url` + an optional `pr_number`). See §Phase 2 → "Strict mode".

### WSE-E4 — Minimal consumption of PR status checks

- `dse_validation/github/ci_status.py` — **T9a**: `consume_ci_status_core` reads
  the PR's check-runs (`GET /repos/{repo}/commits/{ref}/check-runs`) and aggregates
  them into `pending|green|red` (green only if EVERYTHING completed without failure;
  red if any failed; pending while anything is still running — it never "guesses").
  Persists to `wse_ci_status` (Postgres) and returns a `CiStatusResult`. No
  preview, no selective re-run (Phase 3, out of scope). Implemented as an
  **on-demand poll** (the `ACTIVITY_CONSUME_CI_STATUS` Activity is called
  by the WS-B workflow whenever it wants the current status), not as a
  webhook receiver — which is why **there is no `docker-compose.wse.yml`**: WS-E
  exposes no HTTP endpoint in this phase (port 8807 is reserved and unused).
  If the design evolves toward webhooks (lower detection latency), the
  `consume_ci_status_core` Activity does not change — you would only add a thin
  FastAPI receiver in `services/validation/webhook.py` calling the same core function.

### WSE-E6-T15 — Workflow resume on review comment (core of UC4)

- `dse_validation/review_signal.py` — `interpret_review_decision` translates a
  `ConversationEvent` (kind=`approval`, or `review_comment` with a formal
  `review_state` in `source_ref`) into a decision (`approved`|`changes_requested`)
  100% deterministically (P1 — no LLM decides this). `handle_review_event`
  signals the Temporal workflow (`REVIEW_DECISION_SIGNAL_NAME = "review_decision"`)
  using `workflow_id = work_item_id`, and ONLY signals if `interpret_review_decision`
  is not `None` — an ordinary review comment has NO side
  effects (no signal, no WorkItem created, no PR created).
  End-to-end proof in `tests/test_review_signal_e2e.py` against **real
  Temporal** (localhost:7233): it starts a minimal probe workflow, signals via
  `handle_review_event`, and checks that the right workflow (by `workflow_id`)
  resumes with the right decision; it also proves that a "loose" comment does not
  even attempt to signal (returns `False` without calling `get_workflow_handle`/`signal`)
  and does not touch `work_items`/`wse_pr_tracking` in real Postgres.

### Integration with the WS-B Worker

- `dse_validation/activities.py` — `@activity.defn` for the 3 contract
  Activities (`ACTIVITY_RUN_L1_PIPELINE`, `ACTIVITY_FINALIZE_PR`,
  `ACTIVITY_CONSUME_CI_STATUS`), exposed in `ALL_ACTIVITIES` so the single
  Worker in `services/orchestrator/worker.py` can import and register them. Each
  Activity takes 1 pydantic input model (`RunL1PipelineInput`,
  `FinalizePrInput`, `ConsumeCiStatusInput`) — see that file for the exact
  expected shape; since WS-B's real Worker is still under parallel development,
  this is the interface proposed by WS-E, subject to field-name adjustments
  during final integration.

## Phase 2 ("Judgment & queue") — what WS-E added

### WSE-E2-T4 — L2 loop orchestration (fresh context)

The L2 Reviewer **session** (the fresh-context model call) is built
by **WS-C** (WSC-E3-T5) and exposed as the `ACTIVITY_RUN_L2_REVIEW` Activity
(contract name). What **WS-E** owns is the **orchestration** around it:

- `dse_validation/l2/session.py` — `L2ReviewInput` (structural P3: the only
  fields are `work_item_id`, `tenant_id`, `plan`, `diff`, `iteration` — **there is no
  field for the Coder's history/transcript**, so leaking it across the L2 boundary
  is impossible), the `L2ReviewSession` `Protocol`, a deterministic
  `FakeL2ReviewSession` (scriptable, no LLM — used in tests) and `build_l2_session()`,
  which resolves WS-C's real session (`dse_sandbox_runtime.l2`) if importable or
  falls back to the fake with a WARNING (never fails silently — P8).
- `dse_validation/l2/l2_review.py` — `run_l2_review(...)` runs 1 L2 turn, records
  the verdict + cost in `wse_l2_reviews` (evidence, P8) and emits an audit row
  (`l2_review_run`). `guard_l2_after_l1(l1_result)` enforces **cheapest-first (P5)**:
  L2 (model, expensive) only runs after L1 (deterministic, cheap) is green and before
  CI (L3) — trying earlier raises `L2PreconditionError` (clean failure, P6).
- **Order in the flow** (called by the WS-B workflow): L1 -> (green) -> L2 -> (approved)
  -> CI. The P1 gate still holds: no LLM decides flow; the L2 session only **produces**
  a structured `L2Verdict`, and the decision about what to do is the `fix_loop` below.

> **Fake vs. production**: since WS-C is building the session in parallel, the tests
> use `FakeL2ReviewSession` (only the model call is fake; the orchestration —
> recording, cost, guard, P3 — is the production one). As soon as WS-C publishes
> `dse_sandbox_runtime.l2.build_review_session`, `build_l2_session()` starts
> resolving it with no signature change. Alternative integration: WS-B can call
> WS-C's `ACTIVITY_RUN_L2_REVIEW` Activity directly and then the `wse_run_l2_review`
> Activity (WS-E) would only record — today `wse_run_l2_review` does both
> via `build_l2_session()` so there is a testable end-to-end path.

### WSE-E2-T5 — Bounded L2->Coder fix-retry loop

`dse_validation/l2/fix_loop.py` — **100% deterministic** logic (P1) that the
WS-B workflow consults on every L2 verdict (WS-B owns state orchestration;
WS-E supplies the decision):

- `decide_next_action(verdict, state, cfg)` (pure, no I/O): L2 approves ->
  `proceed` (on to CI); L2 rejects and there are retries left **and** budget ->
  `retry_coder` (carries the specific objections back to the Coder); retries
  exhausted -> `escalate_operator`; budget exhausted -> `escalate_operator`
  **even with iterations left (P6)**.
- `register_retry(state, coder_cost_usd, l2_cost_usd, cfg)` — **debits budget**
  and increments the durable counter (`wse_fix_loops`), audits `l2_fix_retry`.
  Belt-and-suspenders P6 guard: it **refuses** to start an iteration if the
  iteration cap OR the cost cap has already been reached (`FixLoopBudgetExceeded`).
- `escalate_to_operator(state, reason, objections)` — marks the loop exhausted
  (durably), audits `l2_fix_loop_exhausted`. Idempotent.
- Config (`config.L2Config`): `DSE_L2_MAX_FIX_RETRIES` (default 3),
  `DSE_L2_BUDGET_CAP_USD` (default 0 = no cost ceiling, only the iteration one).

The counter is durable in Postgres so it survives workflow crash/replay; the
`test_full_bounded_loop_reject_reject_reject_escalate` test exercises the full
cycle (3 rejections -> escalate; the 4th iteration never happens — P6) against
real Postgres.

### WSE-E3-T8 — Strict mode: a human opens the PR

`finalize_pr_core(..., strict_mode=True)` (now wired — the contract gained
`PrRef.compare_url` and an optional `pr_number`). Instead of opening the PR, the finalizer:

1. **pushes** the branch (same GitHub App identity);
2. returns a `PrRef` with `compare_url` populated and `pr_number=None`
   (`compare_url_for(repo, base, branch)` = `.../compare/base...branch?expand=1`);
3. persists tracking with `pr_number NULL` + `compare_url`
   (`wse_pr_tracking`, columns altered in `0012_wse2.sql`);
4. posts the compare link to the **single tracking comment** (via the foundation's
   `MutableCommentWriter` + `GitHubCommentBackend`, `surface="github_pr"`) when
   `surface_ref` is provided;
5. audits `pr_compare_link_posted` (P8).

A human opens the PR with 1 click. When the workflow detects the PR was opened
(webhook/signal from WS-A), it calls `adopt_pr_core(...)` (Activity `wse_adopt_pr`):
it correlates by branch/WorkItem and **adopts** the PR — filling in `pr_number` on the
**same** tracking row (same WorkItem), auditing `pr_adopted`. Idempotent (only the first
human to open wins; re-runs do not overwrite). Re-running `finalize_pr_core` in
strict mode also detects and adopts a PR opened in the meantime.

**Per-repo/tenant flag** (`config.StrictModeConfig.is_strict_for(tenant, repo)`,
most specific wins): `DSE_WSE_STRICT_MODE_TENANT_<T>_<REPO>` >
`DSE_WSE_STRICT_MODE_TENANT_<T>` > `DSE_WSE_STRICT_MODE_REPOS` (`tenant:repo` list)
> `DSE_WSE_STRICT_MODE` (global). Once WS-F publishes the flag in `tenant_config`,
only `is_strict_for` changes to read from there — the signature stays the same.

### New Activities registered (WS-B single Worker)

`dse_validation/activities.py` exposes in `ALL_ACTIVITIES` (in addition to the 3 from Phase 1):

| Name | Input | Returns | Role |
|---|---|---|---|
| `wse_run_l2_review` | `RunL2ReviewInput` (plan+diff only, P3; + `l1_passed` for the P5 guard) | `L2Verdict` | orchestrates the L2 session + records evidence/cost |
| `wse_record_fix_loop` | `RecordFixLoopInput` | `dict` (state) | mirrors the loop's durable counter (WS-B owns the state) |
| `wse_adopt_pr` | `AdoptPrInput` | `PrRef | None` | adopts the human-opened PR (strict mode) |

`ACTIVITY_RUN_L2_REVIEW` (contract) is **not** registered by WS-E — that is the **session**,
owned by WS-C. WS-E's names are prefixed `wse_` so they do not collide in the single Worker.
`FinalizePrInput` gained `strict_mode` (optional; if `None`, resolved via
`StrictModeConfig`) and `surface_ref` (where to post the compare link).

## Phase 3 ("Evidence") — what WS-E added

New infrastructure from this workstream (fragment `docker-compose.wse.yml`, network `dse_net`):

- **`garage`** (`dxflrs/garage:v1.1.0`, pinned) — self-hosted S3 artifact store,
  reserved ports 3900 (S3)/3903 (admin). Single-node dev layout; bootstrap is
  idempotent and done in code (`evidence/garage.ensure_garage_ready`) via the admin API —
  layout, the service's S3 key and a per-tenant bucket. Config in `garage/garage.toml`
  (DEV-ONLY secrets; production = Vault/ESO, WS-F).
- **`wse-gitserver`** (our own image in `gitserver/`, base `alpine:3.20` pinned) —
  git **smart HTTP** (`git-http-backend` behind nginx+fcgiwrap) serving the bare
  preview-manifests repo (`preview_repo/preview-manifests.git`) to the Argo CD
  in the k3d cluster. **Why**: Argo CD's go-git does not speak the dumb protocol
  (a static nginx fails with `unexpected EOF` on ls-remote — observed in practice).
  Fetch-only; the host writes through the filesystem (bind mount).

### WSE-E5-T12 — Garage artifact store

`evidence/garage.py` — Activity `publish_artifact` (CONTRACT name
`ACTIVITY_PUBLISH_ARTIFACT`; input/output `PublishArtifactInput`/`ArtifactRef`
imported, not redefined):

- **per-tenant bucket** (`dse-tenant-<slug>`, NFR-03) + a key prefixed by
  WorkItem (`<wi>/<kind>/<file>`); real upload via boto3; **explicit
  multipart** (create/upload_part/complete) above 5 MiB — validated with a
  **real >5MB mp4 video generated by ffmpeg** and checked byte for byte on the round-trip
  (required by the revised ADR-18).
- **links EXPIRE by policy** (Phase 3 exit criterion): a presigned URL with the TTL from
  the input; a real test proves the expired URL returns **denied** (Garage denies with
  400; AWS would use 403 — documented in the test) and that `resolve_artifact_url`
  refuses with `PermissionError` (P6).
- **QUARANTINE** (EXISTING seam from WS-F, Phase 2): a work item quarantined via
  `dse_platform.kill_switches.quarantine_work_item` (`dse_work_item_quarantine`)
  => `sweep_quarantined_work_items()`/`quarantine_artifacts_for_work_item()` moves
  the objects to the `quarantine/` prefix and **invalidates access before the TTL**
  (the original key ceases to exist — the old URL starts returning 404/403; real test
  with a 1h TTL still in force). The object is preserved (not deleted) for auditing.
- **ACCESS LOG**: every link resolution (`resolve_artifact_url`) writes to
  `wse_artifact_access_log` (correlatable to the PR — input for the *evidence
  consumption* metric) + audit `artifact_link_resolved` (P8).

### WSE-E5-T11 — Playwright @demo video

`evidence/demo.py` — Activity `run_demo_evidence` (contract): runs a REAL `npx
playwright test --grep @demo` (pinned runner `@playwright/test 1.55.1` in
`playwright/`, Chromium installed via `npx playwright install chromium`) with
`video: 'on'` + `trace: 'on'`, publishes the video (.webm) and the trace (.zip) via
`publish_artifact_core` and returns a `DemoEvidenceResult`. The video is verified as
REAL (size>0 + EBML/mp4 header, `is_real_video`). P6: no demo directory
or no `@demo` test => `passed=False` with an explicit detail, never faked evidence.
WS-C's deterministic @demo fixture (WSC-E3-T4b) was being built in
parallel — this workstream's minimal local fixture lives in
`tests/fixtures/demos/wi_demo_fixture/` (static HTML page + `@demo` spec),
path convention `demos/<work_item_id>/`.

### WSE-E4-T10 — Per-PR previews via Argo CD ApplicationSet (REAL k3d cluster)

`preview/paths_filter.py` + `preview/gitops.py` + `preview/argocd.py` —
Activity `trigger_preview` (contract):

- the UI-touching decision comes from a **deterministic paths-filter** (FR-20, fnmatch of
  `files_changed` against `ui_path_globs`; `**/` semantics documented and
  tested). Backend-only => `skipped_backend_only` (success, NEVER blocks).
- when UI-touching: it writes `previews/preview-<wi>/` (Namespace + Deployment
  with pinned `nginx:1.27-alpine` + Service) into the git manifests repo, and the
  **`dse-previews` ApplicationSet** (git generator `previews/*`,
  `requeueAfterSeconds: 15`, `goTemplate`) of the **real Argo CD v2.13.3** creates the
  Application and syncs => ephemeral namespace `preview-<work_item_id>` in the
  `k3d-dse-preview` cluster. Real integration test: namespace created, **URL
  answering HTTP 200** (in-cluster curl probe against the Service), TTL
  tearing it down.
- provisioning failure/timeout => status **`degraded`** (failure mode 9 —
  the PR is never blocked; tested with a nonexistent kubecontext).
- **per-tenant concurrency caps from day 1** (ADR-26): table
  `wse_preview_caps` (default `DSE_PREVIEW_MAX_CONCURRENT`); at the cap =>
  `degraded` with an explicit detail; real counting test against Postgres.
- **TTL reaper — documented decision**: the addendum prefers kube-janitor (P7), but
  with Argo CD in `automated.selfHeal` the source of truth is GIT — kube-janitor
  would delete the namespace and Argo CD would RECREATE it (two controllers fighting).
  The correct reaper under GitOps is `reap_expired_previews()` (a deterministic,
  real Python job): it removes the directory from the repo, the ApplicationSet prunes
  the Application, and the `resources-finalizer` finalizer cascades the namespace
  deletion (proven in the e2e test). kube-janitor remains an upgrade path for
  non-GitOps resources; the `janitor/ttl` annotation is already written on the Namespace.

### WSE-E4-T9b — Full L3

`github/l3.py` — `consume_ci_status_l3` (the `consume_ci_status` Activity gained
the ADDITIVE field `surface_ref`; old WS-B payloads still decode —
the foundation's boundary tests are untouched):

- **reflection** of the aggregated status into the PR's single tracking comment (same
  `MutableCommentWriter` from the foundation, surface `github_pr_ci`, edited in place)
  in the SAME call as the consumption — <1min by construction, measured in the test;
- **targeted re-runs** on a fix commit: a new sha after a `red` state => re-request
  ONLY the failed check-runs (`rerequest_check_run`, actually implemented in
  `RealGitHubClient` against `POST .../check-runs/{id}/rerequest` and exercised
  with `FakeGitHubClient`); a CI without per-job re-run support (403/422) => it
  continues without the re-run, without blocking. Evidence in `wse_ci_reruns` + audit;
- **skill-learning episodes** for CI-repair: a red(sha A) ->
  green(sha B) transition emits a tenant-scoped episode in `wse_ci_repair_episodes` with
  provenance (repo/PR/shas) and the pattern's `occurrence_n`
  (deterministic `failure_signature`). **NO skill is created/activated**
  (checked in the test against `skill_registry`) — promotion is Phase 4.

### WSE-E5-T13/T14 — Visual diff + debounced publication

- `evidence/visual_diff.py` — Activity `run_visual_diff` (contract): **Pillow**
  pixel-diff with per-channel tolerance (8/255, to absorb encoding noise) and a
  percentage threshold; **self-hosted, no SaaS** (Argos/Percy/`toHaveScreenshot`
  = documented upgrade path). Baseline lives in the artifact store (kind
  `visual_baseline`, TTL 30d): the first run creates the baseline
  (`baseline_created=true`); a regression produces a diff image (changed pixels in
  red) published as `visual_diff`. Different sizes = 100% (structural
  change). **Contract field request** (per the addendum's rule — document instead
  of editing the foundation): `VisualDiffResult` has no field to return the key of
  the freshly created baseline; today it comes back in `diff_artifact_key` when
  `baseline_created=true` (documented here and in the docstring) — a dedicated
  `baseline_artifact_key: str | None` would be cleaner.
- `evidence/publication.py` — CONSOLIDATED publication: video/trace/diff/preview/
  CI status in a single tracking comment (surface `github_pr_evidence`), with the
  body re-rendered from DATABASE STATE (crash-consistent); a quarantined artifact
  shows up as revoked, never as a link. **Debounce (ADR-26)**:
  `should_refresh_evidence()` is the 100% deterministic decision consumed by
  the WS-B workflow (Activity `wse_should_refresh_evidence`, returning
  `{"refresh": bool, "reason": str}`): refresh ONLY on an explicit human request or
  a new commit that changes behavior (docs-only and same-commit are debounced);
  every debounce decision audits `evidence_refresh_debounced`.

### New Activities (Phase 3) registered in the single Worker

| Name | Input | Returns | Contract? |
|---|---|---|---|
| `publish_artifact` | `PublishArtifactInput` | `ArtifactRef` | YES (`ACTIVITY_PUBLISH_ARTIFACT`) |
| `run_demo_evidence` | `RunDemoEvidenceInput` | `DemoEvidenceResult` | YES (`ACTIVITY_RUN_DEMO_EVIDENCE`) |
| `trigger_preview` | `TriggerPreviewInput` | `PreviewRef` | YES (`ACTIVITY_TRIGGER_PREVIEW`) |
| `run_visual_diff` | `RunVisualDiffInput` | `VisualDiffResult` | YES (`ACTIVITY_RUN_VISUAL_DIFF`) |
| `wse_quarantine_artifacts` | `QuarantineArtifactsInput` | `list[str]` | no (aux; counterpart to WS-F's kill switch) |
| `wse_reap_previews` | — | `list[str]` | no (aux; WS-B cron/timer) |
| `wse_should_refresh_evidence` | `ShouldRefreshEvidenceInput` | `dict` | no (ADR-26 decision contract for WS-B) |
| `wse_publish_evidence` | `PublishEvidenceInput` | `dict` | no (consolidated publication) |

## Phase 4 ("Loop hardening & learning") — what WS-E added

### WSE-E6-T16 — merge-base, never rebase during review (P0, NEW BUILD)

Finding #2 of addendum 03: merge-base **did not exist** — Phase 1 described it in
the plan but never implemented it; the review loop only re-ran the Coder on the same
branch. Built from scratch in `dse_validation/merge_base.py`, exposed as the
CONTRACT Activity `update_base_branch` (`ACTIVITY_UPDATE_BASE_BRANCH`;
input/output `UpdateBaseBranchInput`/`UpdateBaseBranchResult` imported, not
redefined).

- `update_base_branch_core(...)` — when the base (main) moves ahead during an active
  human review, it updates the task branch by **merge-base-into-branch**
  (`git merge origin/main` ON the task branch) — NEVER rebase+force-push. The
  strategy choice is 100% DETERMINISTIC (P1, code, never a model):
  - no drift → `noop_no_drift`;
  - drift + a human review already happened (`first_human_review_done=True`, the
    contract's **safe default**) OR anchored threads already exist → `merge_base`
    (preserves history → preserves the threads' anchors);
  - drift + no review yet AND zero anchored threads →
    `rebase_prefirst_review` (the only moment when rebase is safe: there is nothing to
    orphan; push with `--force-with-lease`);
  - **belt-and-suspenders (P6)**: even with `first_human_review_done=False`, if
    anchored threads already exist, never rebase — it falls back to `merge_base`.
- **Unresolvable conflict** → `git merge --abort` (or `rebase --abort`),
  returns `conflict=True` (tip unchanged, working tree clean). The WS-B
  workflow escalates to a human — **the agent NEVER force-resolves** (P1/P6).
- **THE PHASE 4 EXIT ASSERTION** (`tests/test_merge_base.py`): it creates a PR with
  base drift + 2 human review threads anchored to commits, applies
  merge-base, and proves `orphaned_threads == 0` by comparing the reachability of the
  anchored shas (`git merge-base --is-ancestor <sha> <branch>`) — merge
  preserves, rebase would break them. A **negative test** (`test_rebase_would_orphan_
  threads_documented_negative`) runs a real rebase and proves that ALL
  threads would be orphaned — documenting why merge-base is mandatory.
- **It does NOT break the anti-AUTOMATIC-merge invariant (FR-16)**: merge-base updates
  the TASK BRANCH with the base's drift (`origin/main → branch`); merging the PR
  into the base remains 100% human. They are opposite operations in direction.
- Durable evidence (P8): `wse_base_updates` (`migrations/0020_wse4.sql`) +
  audit `base_branch_updated` / `base_update_conflict`.

### WSE-E6-T18 — Emitting skill-learning episodes (review feedback)

`dse_validation/review_learning.py` — the 3rd "source at launch" for episodes
(§10.17), alongside CI-repair (Phase 3) and clarification (WS-B). When a
human review feedback is ACCEPTED, it writes a `skill_episode`
(`source='review_feedback'`, the table from migration 0019/WS-C — INSERT/SELECT ONLY):

- `review_pattern_key(comment_body, path)` — a DETERMINISTIC signature (P1):
  string normalization (lowercase + whitespace collapsing) scoped by the path, plus a
  stable short hash — no LLM. Feedback with the same normalized text on the same
  path collides on purpose (that is the "same repeated pattern").
- `record_review_feedback_episode(...)` — `occurrence_n` counts repetitions of the
  SAME `(tenant, source, pattern_key)` (tenant-scoped); full provenance
  (PR, reviewer, path, comment, diff_hunk). **P3**: only feedback ACCEPTED by a
  human becomes an episode (`accepted=False` → nothing). Audit
  `review_feedback_episode_recorded` (P8).
- **BOUNDARY tested** (`tests/test_review_learning.py::test_boundary_no_skill_
  created_or_activated`): recording the episode does NOT create/activate any skill
  (`skill_registry` unchanged before/after). The candidate→eval→
  approved→canary→active promotion is 100% **WS-C**'s (WSC-E4-T2/T3), with human
  approval (P3: no skill self-promotes). WS-C consumes these episodes.
- Exposed as the auxiliary Activity `wse_record_review_episode`
  (`RecordReviewEpisodeInput`; `wse_` prefix, non-contractual).

### Fixture / real / gap — Phase 4

| Component | REAL in this session | Fixture | Gap to production |
|---|---|---|---|
| merge-base | **real git** (local bare repo + clones), real merge/rebase/abort/push, real sha reachability; real Postgres (`wse_base_updates`) + audit | — | The **Activity wrapper** (`_update_base_branch`) resolves the workspace via `MergeBaseConfig` (env `DSE_WSE_GIT_ROOT`) and the anchored threads via the GitHub client's `list_review_threads` — an integration seam with WS-C's sandbox workspace + the real GitHub App (same pending item as Phases 1-3: with no App registered, `FakeGitHubClient.list_review_threads` is a fixture). The core is called directly by the tests with explicit paths (like `LocalFakeSandbox` in L1). |
| review-feedback episodes | real Postgres (`skill_episode`), real occurrence_n/tenant-scope/audit | The accepted feedback is supplied by the caller (WS-B decides "accepted" based on the human review) | The trigger wire in the WS-B workflow: call `wse_record_review_episode` when a `changes_requested` is addressed and the reviewer accepts. Correlating "acceptance" belongs to WS-B/WS-A. |

## Sandbox execution — `SandboxHandle` vs `SandboxExecutor`

`dse_contracts.activities.SandboxHandle` (owner: WS-C) only carries handle
data — it does not define how to RUN a command inside the sandbox. Since
`services/sandbox-runtime` (WS-C) is being built in parallel and may not
publish its own execution interface in time, `dse_validation/sandbox_exec.py`
defines:

- `SandboxExecutor` (Protocol) — `run(argv, cwd=None, timeout=300) -> ExecResult`.
- `DockerExecSandbox` — a REAL implementation via `docker exec <container_id> ...`.
  It works as soon as `SandboxHandle.container_id` is populated by WS-C —
  it depends on nothing else from their runtime.
- `LocalFakeSandbox` — runs the same command via `subprocess` in a local
  directory (no Docker). **Used in ALL of this workstream's tests** to
  prove the L1 pipeline's logic (finding parsing, diff-budget,
  forbidden-paths, SAST, secret-scan) with REAL executions of
  bandit/ruff/mypy/pytest/git — only container isolation is substituted,
  never the tool itself.

If WS-C publishes a richer execution interface, swap out only
`DockerExecSandbox` — `SandboxExecutor` and the L1 pipeline that consumes it do not change.

## Cross-workstream: who is the source of truth for review comments

WS-A (`services/adapter-github`) and WS-E both handle GitHub comments.
This workstream's decision: **WS-A is the source of truth for CORRELATION**
(which `work_item_id`/PR a `ConversationEvent` belongs to, webhook
deduplication, signature verification) and generically decides `new_task` vs
`signal` from the `EventKind`. **WS-E only INTERPRETS THE CONTENT** of the signal once
delivered and correlated — that is, `dse_validation.review_signal.interpret_review_decision`
assumes that `event.source_ref` already contains enough (`repo`, `pr_number`,
`review_state` where applicable) and that the `work_item_id` was already resolved by
WS-A before reaching here. If the real integration shows that `review_state`
is not what WS-A attaches to `source_ref`, it is a 1-function adjustment
(`interpret_review_decision`), not an architectural one.

Symmetrically, the GitHub comment backend for the PR's tracking comment
(`GitHubCommentBackend`, WSE-E3-T7) is our OWN implementation of the
`CommentBackend` Protocol — if `services/adapter-github` has already published an
equivalent backend by the time this code is integrated, the right move is for WS-E to
import that one instead of keeping a second HTTP implementation of the same API
(issue/PR comments). See `dse_validation/github/comment_backend.py`.

## Fixture / local mode vs. what is missing for production

| Component | Local/fixture mode (this session) | What is missing for production |
|---|---|---|
| GitHub App (push, create PR, comments, check-runs) | `FakeGitHubClient` (in-memory, same `GitHubClient` interface) used in all tests — no network/real credentials | Real `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY` (PEM), `GITHUB_APP_INSTALLATION_ID` from a registered GitHub App with `contents:write`, `pull_requests:write`, `issues:write`, `checks:read` and — for repos whose CI reports through the LEGACY commit-status API rather than check runs — `statuses:read`. `statuses:read` is OPTIONAL and its absence is tolerated: `get_combined_status` fails soft, so without it the DSE sees check runs only and a repo that reports exclusively through commit statuses reads as `no_ci`. Adding a permission to an installed App requires the installation owner to approve it before it takes effect. With those 3 env vars present, `build_github_client()` already uses `RealGitHubClient` (genuinely implemented against `https://api.github.com`, JWT RS256 via `PyJWT`+`cryptography`, `httpx`) — it is not pseudocode, it simply was not exercised against real GitHub in this session. |
| `git push` under the App's identity | Tested against a **real local bare repo** (`tests/test_pr_finalizer_idempotent.py::test_push_branch_uses_real_git_against_local_bare_remote`) — real git, no mock, just no network | No logic missing — it only needs the real installation token (which comes from `RealGitHubClient.authenticated_remote_url`, already implemented) |
| `SandboxHandle` → command execution | `LocalFakeSandbox` (local subprocess) in all tests | `SandboxHandle.container_id` populated by WS-C's real runtime; `DockerExecSandbox` is already implemented against real `docker exec`, it just was not exercised against WS-C's runtime (which is being built in parallel) |
| L1 commands (lint/typecheck/test/build) | Generic Python defaults (`ruff check .`, `mypy .`, `pytest -q`, `python -m compileall -q .`), configurable via env (`DSE_L1_*_CMD`) | Production should derive the commands from the TARGET repo itself (the Makefile/package.json/pyproject of the repo the Coder is editing) instead of fixed env vars on the orchestrator process — not implemented (no workstream has yet published a "project manifest" contract) |
| Secret-scan | Our own regex+entropy scanner, real, always available (stdlib) | Could be swapped for `detect-secrets` (already installed in venv-wse as a dev dependency) for broader pattern coverage — the swap is confined to the `secret_scan_check` function, same signature |
| WSE-E3-T8 "strict mode" | **Implemented** (Phase 2) — push + compare link + adoption; tested against real Postgres + `FakeGitHubClient` | Only exercising it against real `api.github.com` is left (same pending item as the normal PR — needs the registered GitHub App) |
| L2 session (model call, fresh context) | `FakeL2ReviewSession` (deterministic, no LLM) — only the orchestration (recording/cost/guard/P3) is production | The real session belongs to WS-C (`dse_sandbox_runtime.l2.build_review_session`), under parallel construction; `build_l2_session()` already resolves it via defensive import once published |
| Interpreting `review_state` in `source_ref` | Assumes WS-A attaches `review_state` (`approved`/`changes_requested`) to formal GitHub review comments | Depends on the actual format `services/adapter-github` (WS-A) produces — a targeted adjustment at integration time |

## Fixture / real / gap — Phase 3 (honesty about what was exercised)

| Component | REAL in this session | Fixture | Gap to production |
|---|---|---|---|
| Garage (S3) | Real `dxflrs/garage:v1.1.0` container, real upload/presign/multipart/copy/delete, expiration policy proven with a real clock | — | Secrets (rpc_secret/admin_token) are dev-only in the repo; production injects them via Vault/ESO (WS-F). Single-node; multi-node is a config change |
| Playwright @demo | Real execution (`npx playwright test --grep @demo`), real Chromium, real webm video + zip trace published to Garage | The demo PAGE is the minimal local fixture (`tests/fixtures/demos/wi_demo_fixture/`) — WS-C is delivering the official fixture in parallel | Run the @demos INSIDE WS-C's sandbox (Playwright in the sandbox image, WSC-E3-T4b) instead of on the host; a real preview `base_url` is already supported in the input |
| Argo CD previews | Real k3d cluster, real Argo CD v2.13.3, real ApplicationSet, namespace created, HTTP 200 URL proven, TTL reap genuinely destroying the namespace | — | `PreviewRef.url` is the in-cluster DNS (`*.svc.cluster.local`) — external exposure (managed ingress/port-forward) is not implemented; the preview image is a static nginx, not the PR's build (that needs the target repo's image pipeline) |
| L3 (reflection/re-runs/episodes) | Real Postgres, real comment store, complete logic | `FakeGitHubClient` (same interface; `RealGitHubClient.rerequest_check_run` is implemented against the real API but there was no registered GitHub App in this session) | Same pending item as Phases 1-2: exercise it against real `api.github.com` |
| Visual diff | Real Pillow, real baseline round-trip through Garage | The tests' screenshots are generated PNGs (not browser captures) — the capture itself belongs to the @demo/preview flow | Wire Playwright screenshot capture into the flow (today the caller supplies the candidate PNG) |
| Publication/debounce | Real Postgres, render from real state, debounce proven | `FakeGitHubClient` for the comment | Same as above: real GitHub App |
| Quarantine | Real seam with `dse_platform` (WS-F) — Phase 2's table and function, audit on both sides | — | Automatic triggering (today `sweep_quarantined_work_items()`/the Activity is called by whoever quarantines, or by cron; missing is WS-F's hook calling the sweep inside `quarantine_work_item` itself, which is their call) |

## Migration

`migrations/0006_wse.sql` (Phase 1, reserved for WS-E) creates: `validation_runs`
(evidence for each L1 run), `wse_pr_tracking` (per-`work_item_id` PR idempotency),
`wse_comment_refs` (the tracking comment's `CommentStateStore`),
`wse_ci_status` (last known CI status).

`migrations/0012_wse2.sql` (Phase 2, reserved for WS-E) adds:
`wse_l2_reviews` (evidence for each L2 turn: verdict, objections, per-iteration
cost), `wse_fix_loops` (durable counter for the bounded L2->Coder loop), and
**alters** `wse_pr_tracking` additively for strict mode (`pr_number`
now accepts NULL + a `compare_url` column). Applied with:

```
DSE_DATABASE_URL=postgresql://dse:dse_dev_only@localhost:5432/dse python3 scripts/migrate.py
```

`migrations/0020_wse4.sql` (Phase 4, reserved for WS-E) adds:
`wse_base_updates` (evidence for each merge-base/rebase: strategy, conflict,
`orphaned_threads`, before/after shas). The review-feedback episodes use the
`skill_episode` table (`source='review_feedback'`) from migration 0019 (WS-C) — WS-E only
does INSERT/SELECT (granted by 0019), no new table for it.

`migrations/0017_wse3.sql` (Phase 3, reserved for WS-E) adds:
`wse_artifacts` (registry of published artifacts + quarantine state),
`wse_artifact_access_log` (PR-correlatable access log — the evidence
consumption metric), `wse_previews` (preview state/TTL), `wse_preview_caps`
(per-tenant ADR-26 caps), `wse_ci_reruns` (targeted re-runs),
`wse_ci_repair_episodes` (tenant-scoped skill-learning episodes) and
`wse_evidence_publications` (ADR-26 debounce state).

## How to run the tests

```bash
python3.12 -m venv /Users/saraiva/Documents/DSE/fase1/.venv-wse
source /Users/saraiva/Documents/DSE/fase1/.venv-wse/bin/activate
pip install -e /Users/saraiva/Documents/DSE/fase1/packages/contracts \
            -e /Users/saraiva/Documents/DSE/fase1/packages/dse_audit \
            -e /Users/saraiva/Documents/DSE/fase1/packages/dse_identity
pip install -e /Users/saraiva/Documents/DSE/fase1/services/validation
pip install -e /Users/saraiva/Documents/DSE/fase1/services/platform  # Phase 3: quarantine seam (WS-F)
pip install pytest pytest-asyncio ruff mypy   # ruff/mypy only so the L1 tests exercise the real defaults

# Phase 3 — pinned Playwright runner + browser (one time):
(cd /Users/saraiva/Documents/DSE/fase1/services/validation/playwright && npm install && npx playwright install chromium)

pytest -q /Users/saraiva/Documents/DSE/fase1/services/validation
```

Requires the foundation infrastructure to be up (Postgres on `localhost:5432` with
`migrations/0001_foundation.sql` AND `0006_wse.sql` applied, Temporal on
`localhost:7233`) — the PR idempotency and audit tests use real Postgres,
and the `review_signal` tests use real Temporal (never mocked,
by design: they are the system's own durability/idempotency guarantees).
**Phase 3 additionally requires**: `0017_wse3.sql` applied; Garage + wse-gitserver up
(`docker compose -f docker-compose.wse.yml up -d --build`); the k3d cluster
`dse-preview` with Argo CD (foundation, `infra/k8s-local/setup-k3d-argocd.sh`);
`ffmpeg` on the host (the >5MB video fixture for the multipart test). The preview
e2e test (`test_preview_e2e_real_cluster_create_serve_and_ttl_reap`) takes
~4-5min (real Argo CD sync + cascade delete).

## Actual result of the last run

```
114 passed in ~274s   (45 Phase 1 + 26 Phase 2 + 32 Phase 3 + 11 Phase 4)
```

Phase 4 added `tests/test_merge_base.py` (6 — including the "zero orphaned threads"
exit assertion + the negative test proving rebase would break them) and
`tests/test_review_learning.py` (5) — real git + real Postgres; audit rows for
`base_branch_updated`/`base_update_conflict`/`review_feedback_episode_recorded`
verified in the real `audit_log` (P8). The "no skill created/activated" boundary
is verified against the real `skill_registry`. No test is mocked or skipped. The
foundation's boundary tests (`test_activity_boundaries.py`, now 15 with the 4 from
Phase 4) still pass UNCHANGED — no workflow call site was modified
by this workstream (the `update_base_branch` Activity uses exactly the contract's
`UpdateBaseBranchInput`/`Result`).

Phase 3 added `tests/test_artifact_store.py` (5), `tests/test_demo_evidence.py`
(3), `tests/test_trigger_preview.py` (8, including the real e2e against k3d),
`tests/test_l3.py` (6), `tests/test_visual_diff.py` (5) and
`tests/test_evidence_publication.py` (5) — real Garage/Postgres/k3d+Argo CD/
Playwright/ffmpeg; GitHub via `FakeGitHubClient` (same interface as the
Real one, see the fixture/real/gap table). Audit rows for `artifact_published`/
`artifact_link_resolved`/`artifact_quarantined`/`demo_evidence_run`/
`preview_created`/`preview_reaped`/`ci_status_reflected`/`ci_targeted_rerun`/
`ci_repair_episode_recorded`/`evidence_published`/`evidence_refresh_debounced`
verified in the real `audit_log` (P8). No test skipped. The foundation's
boundary tests (`packages/contracts/tests/test_activity_boundaries.py`,
11) still pass unchanged — no workflow call site was modified
by this workstream (the new `surface_ref` field on `ConsumeCiStatusInput` is additive and
optional, on WS-E's own model).

Phase 2 added `tests/test_l2_review.py` (6), `tests/test_fix_loop.py` (11),
`tests/test_strict_mode.py` (9) — all against real Postgres (audit rows for
`l2_review_run`/`l2_fix_retry`/`l2_fix_loop_exhausted`/`pr_compare_link_posted`/
`pr_adopted` verified in the real `audit_log`, P8). No test mocks
Postgres/Temporal. No test skipped. Tables use unique `tenant_id`/`work_item_id`
via `uuid4()` per test.

## Known pending items (declared, not hidden)

1. **Real L2 session (WS-C)** — the fresh-context model call is owned by
   WS-C (`ACTIVITY_RUN_L2_REVIEW` / `dse_sandbox_runtime.l2`), under parallel
   construction. `build_l2_session()` already resolves it via defensive import; the tests
   use `FakeL2ReviewSession` (marked as a fixture). The physical fit (cross
   imports, registration in the single Worker) needs an integration pass
   once the merges converge.
2. **Real integration with WS-C** (`DockerExecSandbox`) and **WS-B** (the exact
   field names of the Activities' input models, and who owns the fix-loop counter
   during replay) has not been tested end-to-end for the same reason — the
   interface is ready and documented. Design note: the fix-retry loop's **state**
   belongs to the WS-B workflow (durable via event history); `wse_fix_loops`
   is an evidence/observability mirror. `wse_record_fix_loop` derives the
   pre-iteration state from `iterations`, so a replay that idempotently re-runs
   the Activity converges — but WS-B should treat the counter as
   its own (do not add on top of what the Activity returns).
3. **The real GitHub App** has not been tested against actual `api.github.com` (no
   App registered in this session) — `RealGitHubClient` is implemented against
   the real API, but only `FakeGitHubClient` was exercised in the tests (including
   strict mode and PR adoption).
4. **L1 commands fixed via env** instead of derived from the target repo — see the
   table above.
5. **Strict-mode flag via env** instead of `tenant_config` (WS-F) — once
   the per-tenant flag table exists, change only `StrictModeConfig.is_strict_for`.

Phase 3 (new):

6. **`VisualDiffResult` has no field for the created baseline's key** — a request for a
   new contract field (`baseline_artifact_key`); in the meantime the key comes back
   in `diff_artifact_key` when `baseline_created=true` (documented).
7. **@demo runs on the host, not inside WS-C's sandbox** — once the sandbox
   image ships Playwright (WSC-E3-T4b), `run_demo_evidence_core` gains a
   path via `SandboxExecutor` (the contract input already carries `sandbox`).
8. **The preview URL is in-cluster** (`*.svc.cluster.local`) — external exposure
   (ingress) and a preview image built from the PR (instead of a static nginx)
   are deferred to the integration with the target repo's build pipeline.
9. **The preview reaper is called on demand** (Activity `wse_reap_previews`)
   — WS-B still has to schedule the durable timer/cron; the `janitor/ttl` annotation
   already allows migrating to kube-janitor for non-GitOps resources.
10. **WS-C's official @demo fixture** is under parallel construction — once it publishes
    `demos/<wi>/` in the target repo, this workstream's tests can point there (the
    minimal local fixture remains as a documented fallback).
