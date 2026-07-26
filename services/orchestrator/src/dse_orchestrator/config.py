"""Orchestrator (WS-B) configuration via env var — never hardcoded, so that
tests can speed up timers/caps without editing code (WSB-E3-T1).

Every value has a sane production default; the `temporalio.testing`
(time-skipping) tests pass short values via `WorkItemLifecycleInput`/explicit
config dataclasses instead of mutating environment variables mid-process
(safer under parallelism).
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return float(raw)


def _csv_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return tuple(part.strip().lower() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class OrchestratorConfig:
    """Configurable caps and timers (Phase 1: no fairness/budget — that is WSB-E4/Phase 2)."""

    # WSB-E3-T1 — clarification gate
    clarification_round_cap: int = 3
    clarification_reminder_hours: float = 24.0
    clarification_escalation_days: float = 3.0

    # WSB-E2-T3 — fix loop after L1 fails
    coder_retry_cap: int = 3

    # WSB-E5-T1 — checkpoint/rebuild
    checkpoint_retry_cap: int = 2
    rebuild_retry_cap: int = 1

    # Phase 2 — WSB-E3-T2/T3 (plan approval gate) + WSB-E2 (L2).
    # POLICY for which risk classes require human approval (P1: lives outside
    # the model, here in the operator's config). CSV in
    # DSE_REQUIRE_APPROVAL_RISK_CLASSES (default "high").
    require_approval_risk_classes: tuple[str, ...] = ("high",)
    plan_round_cap: int = 3   # capped re_plan (rejection path)
    l2_retry_cap: int = 2     # L2 objections -> Coder, capped

    # Phase 3 — WSB-E4-T2 (ADR-26): iteration caps + evidence refresh debounce.
    # Configurable via env (no redeploy — the dispatcher fills the workflow
    # input per WorkItem; per-tenant is possible by reading tenant_config
    # before calling apply_to_input).
    review_round_cap: int = 20            # capped human-review/CI-red rounds
    evidence_debounce_seconds: float = 300.0  # window to batch comments (prod)
    evidence_refresh_cap: int = 5         # evidence refreshes beyond the initial one

    # activity timeouts (seconds) — generous because Coder/L1 can be slow;
    # the heartbeat allows detecting a dead worker without waiting out the
    # whole timeout.
    activity_start_to_close_seconds: float = 3600.0
    # ~1 min to detect a dead worker (spec §6); the substrate emits periodic
    # beats, so this short ceiling does not expire legitimately long turns.
    activity_heartbeat_seconds: float = 60.0
    activity_schedule_to_close_seconds: float = 7200.0
    activity_retry_cap: int = 3
    ci_poll_interval_seconds: float = 60.0
    ci_pending_poll_cap: int = 1440

    @classmethod
    def from_env(cls) -> "OrchestratorConfig":
        return cls(
            clarification_round_cap=_int_env("DSE_CLARIFICATION_ROUND_CAP", 3),
            clarification_reminder_hours=_float_env("DSE_CLARIFICATION_REMINDER_HOURS", 24.0),
            clarification_escalation_days=_float_env("DSE_CLARIFICATION_ESCALATION_DAYS", 3.0),
            coder_retry_cap=_int_env("DSE_CODER_RETRY_CAP", 3),
            checkpoint_retry_cap=_int_env("DSE_CHECKPOINT_RETRY_CAP", 2),
            rebuild_retry_cap=_int_env("DSE_REBUILD_RETRY_CAP", 1),
            activity_start_to_close_seconds=_float_env("DSE_ACTIVITY_START_TO_CLOSE_SECONDS", 3600.0),
            activity_heartbeat_seconds=_float_env("DSE_ACTIVITY_HEARTBEAT_SECONDS", 60.0),
            activity_schedule_to_close_seconds=_float_env("DSE_ACTIVITY_SCHEDULE_TO_CLOSE_SECONDS", 7200.0),
            activity_retry_cap=_int_env("DSE_ACTIVITY_RETRY_CAP", 3),
            ci_poll_interval_seconds=_float_env("DSE_CI_POLL_INTERVAL_SECONDS", 60.0),
            ci_pending_poll_cap=_int_env("DSE_CI_PENDING_POLL_CAP", 1440),
            require_approval_risk_classes=_csv_env("DSE_REQUIRE_APPROVAL_RISK_CLASSES", ("high",)),
            plan_round_cap=_int_env("DSE_PLAN_ROUND_CAP", 3),
            l2_retry_cap=_int_env("DSE_L2_RETRY_CAP", 2),
            review_round_cap=_int_env("DSE_REVIEW_ROUND_CAP", 20),
            evidence_debounce_seconds=_float_env("DSE_EVIDENCE_DEBOUNCE_SECONDS", 300.0),
            evidence_refresh_cap=_int_env("DSE_EVIDENCE_REFRESH_CAP", 5),
        )


DEFAULT_CONFIG = OrchestratorConfig()


def apply_to_input(input, cfg: "OrchestratorConfig | None" = None):
    """Fill the cap/timer fields of a `WorkItemLifecycleInput` from `cfg`
    (default: `OrchestratorConfig.from_env()`). Called by whoever starts the
    workflow (e.g. the WS-A dispatcher, or this service's `worker.py`/example
    scripts) — never by the workflow itself (env vars are not read inside the
    workflow sandbox)."""
    cfg = cfg or OrchestratorConfig.from_env()
    input.clarification_round_cap = cfg.clarification_round_cap
    input.clarification_reminder_hours = cfg.clarification_reminder_hours
    input.clarification_escalation_days = cfg.clarification_escalation_days
    input.coder_retry_cap = cfg.coder_retry_cap
    input.checkpoint_retry_cap = cfg.checkpoint_retry_cap
    input.rebuild_retry_cap = cfg.rebuild_retry_cap
    input.activity_start_to_close_seconds = cfg.activity_start_to_close_seconds
    input.activity_heartbeat_seconds = cfg.activity_heartbeat_seconds
    input.activity_schedule_to_close_seconds = cfg.activity_schedule_to_close_seconds
    input.activity_retry_cap = cfg.activity_retry_cap
    input.ci_poll_interval_seconds = cfg.ci_poll_interval_seconds
    input.ci_pending_poll_cap = cfg.ci_pending_poll_cap
    input.require_approval_risk_classes = cfg.require_approval_risk_classes
    input.plan_round_cap = cfg.plan_round_cap
    input.l2_retry_cap = cfg.l2_retry_cap
    input.review_round_cap = cfg.review_round_cap
    input.evidence_debounce_seconds = cfg.evidence_debounce_seconds
    input.evidence_refresh_cap = cfg.evidence_refresh_cap
    return input
