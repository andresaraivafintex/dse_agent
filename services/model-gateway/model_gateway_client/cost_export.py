"""Cost aggregation by tenant/task-class/stage (WSD-E3-T2 / WSD-E3-T4).

DURABLE source (the default from Phase 2 on — WSD-E3-T4): the
`model_call_ledger` table (`ledger.py`), written on every successful call with
LiteLLM's REAL cost/tokens. It survives a process restart and aggregates across
processes — open item #4 from the addendum ("today in memory, per process") is
resolved. `aggregate_cost(..., source="ledger")` (the default) reads from it.

In-memory source (Phase 1 legacy, still available via `source="memory"`): the
spans from the current process's `InMemorySpanExporter`
(`telemetry.get_recorded_spans()`). Useful for pure unit tests that do not want
to touch Postgres.

OTel spans keep being exported in parallel to the WS-F collector when
`DSE_OTEL_EXPORTER_OTLP_ENDPOINT` is set (WSF-E7 dashboards/alerting). The
local collector only does `debug`/stdout (no queryable backend in this
environment), so the queryable aggregation lives in the Postgres ledger, not in
the collector — see README §"WSD-E3-T4".
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from dse_contracts.constants import (
    OTEL_ATTR_COST_USD,
    OTEL_ATTR_MODEL,
    OTEL_ATTR_STAGE,
    OTEL_ATTR_TENANT,
    OTEL_ATTR_TOKENS_IN,
    OTEL_ATTR_TOKENS_OUT,
)

from . import ledger, telemetry
from .telemetry import OTEL_ATTR_TASK_CLASS


@dataclass
class CostBucket:
    tenant_id: str
    task_class: str
    stage: str
    call_count: int = 0
    total_cost_usd: float = 0.0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    models: set[str] = field(default_factory=set)

    def as_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "task_class": self.task_class,
            "stage": self.stage,
            "call_count": self.call_count,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "total_tokens_in": self.total_tokens_in,
            "total_tokens_out": self.total_tokens_out,
            "models": sorted(self.models),
        }


def _iter_spans():
    """In-memory buffer of the current process (source="memory"). Phase 1
    legacy source — see the module docstring."""
    return telemetry.get_recorded_spans()


def aggregate_cost(*, tenant_id: str | None = None, source: str = "ledger") -> list[dict]:
    """Aggregates cost/tokens by (tenant_id, task_class, stage). If `tenant_id`
    is passed, filters to that tenant only (isolation — never returns another
    tenant's data mixed in by accident).

    `source`:
      - "ledger" (default, WSD-E3-T4): the durable `model_call_ledger` table;
      - "memory": spans from the current process (Phase 1 legacy)."""
    if source == "ledger":
        return ledger.aggregate(tenant_id=tenant_id)
    if source != "memory":
        raise ValueError(f"invalid source: {source!r} (use 'ledger' or 'memory')")

    buckets: dict[tuple[str, str, str], CostBucket] = {}

    for span in _iter_spans():
        attrs = span.attributes or {}
        span_tenant = attrs.get(OTEL_ATTR_TENANT)
        if span_tenant is None:
            continue
        if tenant_id is not None and span_tenant != tenant_id:
            continue
        span_stage = attrs.get(OTEL_ATTR_STAGE, "unknown")
        span_task_class = attrs.get(OTEL_ATTR_TASK_CLASS, "default")
        key = (span_tenant, span_task_class, span_stage)

        bucket = buckets.get(key)
        if bucket is None:
            bucket = CostBucket(tenant_id=span_tenant, task_class=span_task_class, stage=span_stage)
            buckets[key] = bucket

        bucket.call_count += 1
        bucket.total_cost_usd += float(attrs.get(OTEL_ATTR_COST_USD, 0.0) or 0.0)
        bucket.total_tokens_in += int(attrs.get(OTEL_ATTR_TOKENS_IN, 0) or 0)
        bucket.total_tokens_out += int(attrs.get(OTEL_ATTR_TOKENS_OUT, 0) or 0)
        model = attrs.get(OTEL_ATTR_MODEL)
        if model:
            bucket.models.add(model)

    return [b.as_dict() for b in sorted(buckets.values(), key=lambda b: (b.tenant_id, b.task_class, b.stage))]


def aggregate_cost_by_tenant() -> dict[str, float]:
    """Shortcut: total cost per tenant (sum over all task_class/stage)."""
    totals: dict[str, float] = defaultdict(float)
    for row in aggregate_cost():
        totals[row["tenant_id"]] += row["total_cost_usd"]
    return dict(totals)
