"""OTel instrumentation for the model-gateway client (WSD-E3-T1/T2).

Every model call made through `gateway_call.chat_completion` produces a
`dse.model_gateway.chat_completion` span with the attributes defined in
`dse_contracts.constants` (stable contract between WS-D and WS-F):
`OTEL_ATTR_TENANT/WORK_ITEM/STAGE/MODEL/COST_USD/TOKENS_IN/TOKENS_OUT`, filled
from LiteLLM's real response (it already returns cost/tokens — we recompute
nothing here).

Two export destinations, always active at the same time:
  1. An `InMemorySpanExporter` — always on. It is what makes `WSD-E3-T2` (cost
     aggregation) and the pytest suite itself testable without depending on any
     running collector. Read it via `get_recorded_spans()`.
  2. A real OTLP/HTTP exporter to the WS-F collector, IF
     `DSE_OTEL_EXPORTER_OTLP_ENDPOINT` is set. In this session there is no WS-F
     collector up (services/platform/ has not published one yet) — we document
     the expected integration but it is conditional/opt-in and breaks nothing
     when absent.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from dse_contracts.constants import (
    OTEL_ATTR_COST_USD,
    OTEL_ATTR_MODEL,
    OTEL_ATTR_STAGE,
    OTEL_ATTR_TENANT,
    OTEL_ATTR_TOKENS_IN,
    OTEL_ATTR_TOKENS_OUT,
    OTEL_ATTR_WORK_ITEM,
)
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from . import settings

# Extra attribute, outside the contract published in dse_contracts.constants
# (which defines no OTEL_ATTR_TASK_CLASS today) — but required for the cost
# aggregation by tenant/task-class/stage asked for in WSD-E3-T2. It follows the
# same naming convention (`dse.<field>`) and is additive: it replaces no
# contract attribute, it only complements them. Documented in the README as a
# field that should be promoted to the shared contract when WS-F touches it.
OTEL_ATTR_TASK_CLASS = "dse.task_class"

_IN_MEMORY_EXPORTER = InMemorySpanExporter()
_PROVIDER: TracerProvider | None = None


def _build_provider() -> TracerProvider:
    provider = TracerProvider(
        resource=Resource.create({"service.name": "dse-model-gateway-client"})
    )
    # Always on — it is the backend used by the tests and by the local cost_export.
    provider.add_span_processor(SimpleSpanProcessor(_IN_MEMORY_EXPORTER))

    endpoint = settings.otlp_exporter_endpoint()
    if endpoint:
        # Expected production path: WS-F's OTel collector. Local import so we
        # do not force the network dependency on anyone who configured nothing.
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
        )
    return provider


def get_tracer_provider() -> TracerProvider:
    global _PROVIDER
    if _PROVIDER is None:
        _PROVIDER = _build_provider()
    return _PROVIDER


def get_recorded_spans() -> tuple[ReadableSpan, ...]:
    """All spans emitted by the current process — used by tests and by
    `cost_export.py`. In production the source of truth is the collector
    backend (WS-F's Tempo/Jaeger/etc.), not this in-memory buffer."""
    get_tracer_provider()  # makes sure the provider (and the processor) exists
    return tuple(_IN_MEMORY_EXPORTER.get_finished_spans())


def clear_recorded_spans() -> None:
    _IN_MEMORY_EXPORTER.clear()


@contextmanager
def model_call_span(
    *,
    tenant_id: str,
    work_item_id: str,
    stage: str,
    model: str,
    task_class: str | None = None,
) -> Iterator[trace.Span]:
    """Span for one model call. Cost/token attributes are set by the caller
    (`gateway_call.chat_completion`) after LiteLLM's response arrives — only it
    knows the real cost of that specific call."""
    tracer = trace.get_tracer("dse.model_gateway_client", tracer_provider=get_tracer_provider())
    with tracer.start_as_current_span("dse.model_gateway.chat_completion") as span:
        span.set_attribute(OTEL_ATTR_TENANT, tenant_id)
        span.set_attribute(OTEL_ATTR_WORK_ITEM, work_item_id)
        span.set_attribute(OTEL_ATTR_STAGE, stage)
        span.set_attribute(OTEL_ATTR_MODEL, model)
        if task_class is not None:
            span.set_attribute(OTEL_ATTR_TASK_CLASS, task_class)
        yield span


def set_usage_attributes(
    span: trace.Span, *, model: str, cost_usd: float, tokens_in: int, tokens_out: int
) -> None:
    span.set_attribute(OTEL_ATTR_MODEL, model)
    span.set_attribute(OTEL_ATTR_COST_USD, cost_usd)
    span.set_attribute(OTEL_ATTR_TOKENS_IN, tokens_in)
    span.set_attribute(OTEL_ATTR_TOKENS_OUT, tokens_out)
