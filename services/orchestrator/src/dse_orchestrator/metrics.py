"""Phase 3 — feeds the alert for approaching Temporal's history limit
(infra/ALERTING-RULES.md §3, together with WS-F).

The Temporal server emits `temporal_workflow_event_history_size` natively, but
the foundation's compose does not expose the server's Prometheus metrics yet
(explicit TODO in rule §3). In the meantime, the orchestrator itself emits an
OTel metric with the APPROXIMATE size of the current run, measured worker-side:

  - `dse.workflow.history_length`  — `workflow.info().get_current_history_length()`
    (the real event count of the current run, exposed by the SDK in a
    deterministic/replay-safe way);
  - `dse.workflow.history_size_bytes` — `get_current_history_size()` (bytes);
  - `dse.workflow.continue_as_new_count` — how many times this chain of
    executions has done Continue-As-New (the current run "resets" the history on
    every CAN; the count gives the context of how many resets happened).

Attributes: `dse.work_item_id`, `dse.tenant_id`, `dse.stage` (workflow phase)
and `dse.checkpoint` (the boundary that emitted). WS-F points the alert rule
(Warning 70% / Critical 90% of ~10k events) at `dse.workflow.history_length`.

The READ happens inside the workflow (deterministic); the EMISSION happens in
the local Activity `emit_history_metric` (I/O outside the sandbox — P1
discipline).

Exporter configurable through the SAME env as tracing (`DSE_OTEL_EXPORTER`):
console (local default) or otlp + `DSE_OTEL_EXPORTER_OTLP_ENDPOINT` pointing at
WS-F's otel-collector (`otel-collector:4317` in compose).
"""
from __future__ import annotations

import logging
import os
import threading

from dse_contracts.constants import OTEL_ATTR_STAGE, OTEL_ATTR_TENANT, OTEL_ATTR_WORK_ITEM

logger = logging.getLogger("dse_orchestrator.metrics")

_SERVICE_NAME = "dse-orchestrator"

METRIC_HISTORY_LENGTH = "dse.workflow.history_length"
METRIC_HISTORY_SIZE_BYTES = "dse.workflow.history_size_bytes"
METRIC_CONTINUE_AS_NEW = "dse.workflow.continue_as_new_count"
ATTR_CHECKPOINT = "dse.checkpoint"

# Phase 4 — PR QUALITY metrics (pilot gate "PR quality thresholds", addendum 03
# Part 3). The internal pilot measures the health of the review loop per PR;
# these four feed that gate. Real NUMBERS only come from operating against real
# repos (administrative blocker in addendum 03) — here we guarantee the
# instrumentation exists and emits deterministically from the workflow side.
METRIC_PR_REVIEW_ROUNDS = "dse.pr.review_rounds"          # review rounds per PR
METRIC_PR_CHANGES_REQUESTED = "dse.pr.changes_requested_total"  # changes_requested rate (count)
METRIC_PR_TIME_TO_MERGE = "dse.pr.time_to_merge_seconds"  # time to merge (finalize -> merged_by_human)
METRIC_PR_EVIDENCE_REFRESHES = "dse.pr.evidence_refreshes"  # evidence-consumption (proxy; WS-E logs the real access)
ATTR_PR_OUTCOME = "dse.pr.outcome"  # "merged" | "escalated" | ...

_lock = threading.Lock()
_meter = None
_hist_length = None
_hist_size = None
_can_count = None
_pr_review_rounds = None
_pr_changes_requested = None
_pr_time_to_merge = None
_pr_evidence_refreshes = None


def _build_metric_reader():
    from opentelemetry.sdk.metrics.export import (
        ConsoleMetricExporter,
        MetricExportResult,
        PeriodicExportingMetricReader,
    )

    class _QuietConsoleMetricExporter(ConsoleMetricExporter):
        """Console exporter that tolerates an already-closed stdout (e.g. a
        shutdown flush after pytest closed the capture) — metrics are always
        best-effort, never fatal noise."""

        def export(self, metrics_data, timeout_millis: float = 10_000, **kwargs):
            try:
                return super().export(metrics_data, timeout_millis=timeout_millis, **kwargs)
            except ValueError:
                return MetricExportResult.SUCCESS

    kind = os.environ.get("DSE_OTEL_EXPORTER", "console").strip().lower()
    if kind == "otlp":
        endpoint = os.environ.get("DSE_OTEL_EXPORTER_OTLP_ENDPOINT")
        if not endpoint:
            logger.warning(
                "DSE_OTEL_EXPORTER=otlp but DSE_OTEL_EXPORTER_OTLP_ENDPOINT is not set; "
                "metrics fall back to ConsoleMetricExporter (local mode)."
            )
        else:
            try:
                from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
                    OTLPMetricExporter,
                )

                return PeriodicExportingMetricReader(
                    OTLPMetricExporter(endpoint=endpoint, insecure=True),
                    export_interval_millis=15_000,
                )
            except ImportError:
                logger.warning(
                    "opentelemetry-exporter-otlp-proto-grpc is not installed; metrics "
                    "fall back to ConsoleMetricExporter. Install it for production."
                )
    return PeriodicExportingMetricReader(
        _QuietConsoleMetricExporter(), export_interval_millis=60_000
    )


def _make_instruments() -> None:
    global _hist_length, _hist_size, _can_count
    _hist_length = _meter.create_histogram(
        METRIC_HISTORY_LENGTH,
        unit="{event}",
        description="Events in the CURRENT WorkItemLifecycleWorkflow run's history "
        "(get_current_history_length) — rule §3 of ALERTING-RULES.md",
    )
    _hist_size = _meter.create_histogram(
        METRIC_HISTORY_SIZE_BYTES,
        unit="By",
        description="Bytes of the current run's history (get_current_history_size)",
    )
    _can_count = _meter.create_histogram(
        METRIC_CONTINUE_AS_NEW,
        unit="{run}",
        description="How many Continue-As-New this chain of executions has already done",
    )
    global _pr_review_rounds, _pr_changes_requested, _pr_time_to_merge, _pr_evidence_refreshes
    _pr_review_rounds = _meter.create_histogram(
        METRIC_PR_REVIEW_ROUNDS, unit="{round}",
        description="Human-review/CI-red rounds per PR (pilot gate: PR quality thresholds)",
    )
    _pr_changes_requested = _meter.create_histogram(
        METRIC_PR_CHANGES_REQUESTED, unit="{batch}",
        description="How many changes_requested batches a PR has accumulated (pilot gate)",
    )
    _pr_time_to_merge = _meter.create_histogram(
        METRIC_PR_TIME_TO_MERGE, unit="s",
        description="Seconds from PR finalized to merged_by_human (pilot gate)",
    )
    _pr_evidence_refreshes = _meter.create_histogram(
        METRIC_PR_EVIDENCE_REFRESHES, unit="{refresh}",
        description="PR evidence refreshes (evidence-consumption proxy; "
        "the real access is logged by WS-E). Pilot gate.",
    )


def _ensure_configured() -> None:
    global _meter
    with _lock:
        if _meter is not None:
            return
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.resources import Resource

        provider = MeterProvider(
            resource=Resource.create({"service.name": _SERVICE_NAME}),
            metric_readers=[_build_metric_reader()],
        )
        _meter = provider.get_meter(_SERVICE_NAME)
        _make_instruments()


def configure_for_tests(metric_reader) -> None:
    """Injects a MetricReader (e.g. InMemoryMetricReader) — test-only use; it
    replaces any previous configuration of this module."""
    global _meter
    from opentelemetry.sdk.metrics import MeterProvider

    with _lock:
        provider = MeterProvider(metric_readers=[metric_reader])
        _meter = provider.get_meter(_SERVICE_NAME)
        _make_instruments()


def record_history_metric(
    *,
    work_item_id: str,
    tenant_id: str,
    phase: str,
    checkpoint: str,
    history_length: int,
    history_size_bytes: int = 0,
    continue_as_new_count: int = 0,
) -> None:
    _ensure_configured()
    attrs = {
        OTEL_ATTR_WORK_ITEM: work_item_id,
        OTEL_ATTR_TENANT: tenant_id,
        OTEL_ATTR_STAGE: phase,
        ATTR_CHECKPOINT: checkpoint,
    }
    _hist_length.record(int(history_length), attrs)
    if history_size_bytes:
        _hist_size.record(int(history_size_bytes), attrs)
    _can_count.record(int(continue_as_new_count), attrs)


def record_pr_quality_metric(
    *,
    work_item_id: str,
    tenant_id: str,
    outcome: str,
    review_rounds: int,
    changes_requested_count: int,
    evidence_refreshes: int,
    time_to_merge_seconds: float | None = None,
) -> None:
    """Phase 4 — emits the four PR QUALITY metrics that feed the pilot gate "PR
    quality thresholds" (addendum 03). Emitted at a TERMINAL PR boundary (merge
    or escalation) with the run's final tallies, plus incrementally per round
    (counters) so data on PRs that never merge is not lost. Best-effort — a
    failure here never affects the flow."""
    _ensure_configured()
    attrs = {
        OTEL_ATTR_WORK_ITEM: work_item_id,
        OTEL_ATTR_TENANT: tenant_id,
        ATTR_PR_OUTCOME: outcome,
    }
    _pr_review_rounds.record(int(review_rounds), attrs)
    _pr_changes_requested.record(int(changes_requested_count), attrs)
    _pr_evidence_refreshes.record(int(evidence_refreshes), attrs)
    if time_to_merge_seconds is not None:
        _pr_time_to_merge.record(float(time_to_merge_seconds), attrs)
