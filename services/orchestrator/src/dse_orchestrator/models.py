"""Types that cross the workflow boundary (`@workflow.run` input/output and
`continue_as_new`). Plain dataclasses (not pydantic) on purpose: the workflow
runs inside the Temporal Python SDK's deterministic sandbox, and dataclasses
of primitive types are the best-supported path for the default data converter
without extra plugins. Rich pydantic types (WorkItem, PlanArtifact, the types
in `dse_contracts.activities`) are used freely in *Activity params/returns*,
which run outside the sandbox.
"""
from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Coarse workflow phases — each one closes with `continue_as_new` to keep the
# event history small (WSB-E2-T1).
# ---------------------------------------------------------------------------
PHASE_INTAKE = "intake"
PHASE_IMPLEMENTATION = "implementation"
PHASE_REVIEW = "review"
PHASE_TERMINAL = "terminal"


@dataclass
class WorkItemLifecycleInput:
    """State carried across `continue_as_new` between phases. `status` uses the
    (string) values of `dse_contracts.work_item.WorkItemStatus`."""

    work_item_id: str
    tenant_id: str
    requester: str
    repo: str | None = None
    base_branch: str | None = None
    data_class: str = "internal"
    acceptance_criteria: str | None = None
    # S1 (Phase 5): issue title+body (already sanitized) — WHAT to build.
    # Loaded from ingest_events.payload by load_work_item; passed as the
    # Planner/Coder instruction. Without it the agents did not know the task.
    task_content: str = ""
    # Source issue number (from work_items.source_ref, via load_work_item).
    # finalize uses it as issue_ref -> "Closes #N" in the PR body (back-link +
    # auto-close on merge). Without it the PR went out with "(sem issue de
    # origem vinculada)" even when it came from an issue.
    issue_number: int | None = None

    phase: str = PHASE_INTAKE
    status: str = "new"

    clarification_rounds: int = 0
    coder_retry_count: int = 0
    review_round: int = 0

    sandbox_id: str | None = None
    # S7 (Phase 5): serialized SandboxHandle (sandbox_id, work_item_id,
    # tenant_id, branch, container_id) retained from provision. L1 and finalize
    # need the COMPLETE handle (not just sandbox_id) to resolve the executor —
    # the old call site only kept sandbox_id, causing `Failed decoding arguments`
    # (missing RunL1PipelineInput.sandbox). Survives continue_as_new.
    sandbox_handle: dict = field(default_factory=dict)
    # Last successful CheckpointRef (dict) — the rebuild source. Lives in the
    # INPUT (not in an instance attribute) so it survives continue_as_new: a
    # rebuild in the review phase restores from the implementation checkpoint.
    last_checkpoint_ref: dict = field(default_factory=dict)
    branch: str | None = None
    # Immutable SHAs delimit every gate. Defaults preserve old histories; new
    # executions capture base_sha before the first turn and head_sha at every
    # checkpoint prior to L1.
    base_sha: str | None = None
    head_sha: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    ci_status: str | None = None
    state_version: int = 0

    # accumulated textual payload of clarification answers (for the Coder to
    # eventually consume via Activity — Phase 1 does not interpret it with an
    # LLM inside the workflow, it only forwards it; P1: no flow decision by LLM).
    clarification_notes: list[str] = field(default_factory=list)

    terminal_detail: str | None = None

    # ------------------------------------------------------------------
    # Phase 2 — session split + plan approval gate (extended WSB-E2-T3 /
    # WSB-E3-T2/T3). These survive `continue_as_new` like the rest of the
    # workflow's deterministic state.
    # ------------------------------------------------------------------
    # Serialized PlanArtifact (produced by the read-only Planner BEFORE the
    # gate). Passed to Reviewer L2 together with the final diff — ONLY this +
    # the diff, never the Coder's history (P3).
    plan_json: dict = field(default_factory=dict)
    plan_hash: str | None = None
    # EFFECTIVE risk class (planner + deterministic defense-in-depth
    # classification — see policy.classify_risk). Drives the gate.
    risk_class: str = "low"
    # approvers resolved by the CODEOWNERS -> access bundle (WS-F) cascade,
    # filled in when the gate parks at awaiting_plan_approval.
    approvers: list[str] = field(default_factory=list)
    plan_rounds: int = 0  # how many times the Planner ran (re_plan increments)

    # Reviewer L2 (fresh context) — objections go back to the Coder, capped.
    l2_objections: list[str] = field(default_factory=list)
    l2_retry_count: int = 0

    # ------------------------------------------------------------------
    # Phase 2 — budgets (WSB-E4-T1). `budget_max_usd` comes from the JSONB
    # `work_items.budget` (key "max_usd") read at admission; `spent_usd`
    # accumulates the cost reported by the gateway (WS-D) on every model
    # Activity. Checked at admission and at EVERY phase boundary — it never
    # cuts mid-Activity (P6).
    # ------------------------------------------------------------------
    budget_max_usd: float | None = None
    spent_usd: float = 0.0

    # ------------------------------------------------------------------
    # Configurable caps/timers (WSB-E3-T1 / E2-T3 / E5-T1). They are part of the
    # workflow INPUT (not env vars read inside the sandbox) so that:
    #  (a) they are deterministic and survive `continue_as_new`/replay;
    #  (b) tests can inject short timers without monkeypatching env inside the
    #      workflow sandbox (which is neither safe nor supported).
    # `dse_orchestrator.config.OrchestratorConfig.from_env()` is the helper that
    # whoever starts the workflow (worker/WS-A dispatcher) uses to fill these
    # fields from the production environment.
    # ------------------------------------------------------------------
    clarification_round_cap: int = 3
    clarification_reminder_hours: float = 24.0
    clarification_escalation_days: float = 3.0
    coder_retry_cap: int = 3
    checkpoint_retry_cap: int = 2
    rebuild_retry_cap: int = 1
    activity_start_to_close_seconds: float = 3600.0
    # Remediation (spec §6): the substrate emits a periodic heartbeat during
    # long turns (sandbox_runtime.activity_heartbeat.run_sync_with_heartbeat).
    # 2026-07-23 (real runs wi_d05c/wi_257d/wi_150d): at 60s, coder/tester turns
    # with model calls fell into an INFINITE retry LOOP — the server cancelled
    # on heartbeat timeout an ACTIVITY THAT WAS STILL WORKING (and the retry
    # paid for the model again). 600s gives real slack; start_to_close (1h)
    # remains the hard ceiling of the turn. NOTE: this default IS the operative
    # value on the dispatcher path (start_workflow with a string → _coerce_input
    # uses the model default; apply_to_input/env only applies to callers that
    # pass the full input).
    activity_heartbeat_seconds: float = 600.0
    activity_schedule_to_close_seconds: float = 7200.0
    # Retries of business Activities must terminate. schedule-to-close remains
    # the time ceiling, and this cap bounds attempts/cost.
    activity_retry_cap: int = 3

    # CI pending is a durable wait, not permission to review. The default cap
    # equals 24h at one poll per minute; both live in the input for
    # deterministic replay and can be shortened in tests.
    ci_poll_interval_seconds: float = 60.0
    ci_pending_poll_cap: int = 1440
    ci_pending_polls: int = 0

    # Phase 2 (WSB-E3-T2/E3-T3/E4) — new caps/policy. `require_approval_risk_classes`
    # is the POLICY of which risk classes require human approval; it lives in the
    # INPUT (outside the model — P1), filled by config.from_env by the caller.
    require_approval_risk_classes: tuple[str, ...] = ("high",)
    plan_round_cap: int = 3  # capped re_plan (rejection path — WSB-E3-T3)
    l2_retry_cap: int = 2  # L2 objections -> Coder, capped

    # ------------------------------------------------------------------
    # Phase 3 — WSB-E4-T2: iteration caps + evidence refresh debounce (ADR-26)
    # and the evidence pipeline state (preview/demo/visual diff) that survives
    # `continue_as_new` like the rest of the deterministic state.
    # ------------------------------------------------------------------
    # Cap on review rounds (human changes_requested/CI-red loop). Every `while`
    # in the workflow has a cap: clarification (clarification_round_cap), L1 fix
    # (coder_retry_cap), L2 objections (l2_retry_cap), re_plan (plan_round_cap)
    # and now review rounds. Exhausted -> escalated.
    review_round_cap: int = 20
    # Debounce window (seconds) to batch review comments before ONE fix cycle +
    # ONE evidence refresh (ADR-26: 6 comments in one window = at most 1
    # refresh). 0 = no window (compat/legacy tests); production gets the value
    # from config.from_env via apply_to_input.
    evidence_debounce_seconds: float = 0.0
    # Cap on evidence refreshes BEYOND the initial one. Exceeded -> clean
    # decline (audited, P6) without blocking the PR — the evidence just goes
    # "stale".
    evidence_refresh_cap: int = 5
    evidence_refreshes: int = 0

    # Evidence pipeline state (also projected into work_item_evidence,
    # migration 0014, via the local Activity record_evidence_state).
    preview_status: str | None = None   # PreviewRef.status
    preview_url: str | None = None
    evidence_passed: bool | None = None
    evidence_video_key: str | None = None
    evidence_trace_key: str | None = None
    visual_baseline_key: str | None = None
    # last known files_changed (initial Coder or last fix cycle) — a
    # human-requested refresh has no new commit, so it reuses this set for
    # trigger_preview's deterministic paths-filter decision (FR-20).
    last_files_changed: list[str] = field(default_factory=list)

    # Feeds the history alert (infra/ALERTING-RULES.md §3, with WS-F):
    # Continue-As-New count for this chain of executions, emitted together with
    # the history length/size at each boundary via emit_history_metric.
    continue_as_new_count: int = 0

    # ------------------------------------------------------------------
    # Phase 4 — PR quality metrics (pilot gate "PR quality thresholds",
    # addendum 03). Deterministic counters that survive `continue_as_new` like
    # the rest of the state; emitted via OTel at the PR's terminal boundary
    # (merge/escalation) by the local Activity emit_pr_quality_metric.
    # ------------------------------------------------------------------
    changes_requested_count: int = 0
    # epoch (seconds) of when the PR was finalized (workflow.now() —
    # deterministic/replay-safe read). Used for time-to-merge; None until the PR
    # exists.
    pr_finalized_at_epoch: float | None = None


@dataclass
class WorkItemLifecycleResult:
    work_item_id: str
    status: str
    detail: str | None = None
    pr_number: int | None = None


@dataclass
class OperatorEvent:
    """Latest operator events, exposed via query for observability."""

    action: str
    actor: str
    reason: str | None = None
