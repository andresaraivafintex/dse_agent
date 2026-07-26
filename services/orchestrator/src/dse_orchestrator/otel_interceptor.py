"""WSB-E1-T4 — OpenTelemetry trace propagation from workflow to Activity.

Instead of reinventing context propagation (fragile inside the workflow's
deterministic sandbox), we use the official
`temporalio.contrib.opentelemetry.TracingInterceptor`, which already handles:
  - span creation per workflow run / activity execution;
  - trace context propagation via Temporal `headers` (the same channel
    used for other cross-boundary metadata);
  - compatibility with the workflow sandbox (does not call
    non-deterministic APIs inside `@workflow.run`).

`setup_tracing()` builds a real `TracerProvider` (opentelemetry-sdk) with an
exporter configurable via env:
  - `DSE_OTEL_EXPORTER=console` (default, local/dev mode without real
    observability infra) — prints spans to the worker's stdout;
  - `DSE_OTEL_EXPORTER=otlp` + `DSE_OTEL_EXPORTER_OTLP_ENDPOINT=<host:port>`
    — for production, once a real OTLP collector (e.g. WS-F's) exists.
"""
from __future__ import annotations

import logging
import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from temporalio.contrib.opentelemetry import TracingInterceptor

logger = logging.getLogger("dse_orchestrator.otel")

_SERVICE_NAME = "dse-orchestrator"


def _build_exporter():
    kind = os.environ.get("DSE_OTEL_EXPORTER", "console").strip().lower()
    if kind == "otlp":
        endpoint = os.environ.get("DSE_OTEL_EXPORTER_OTLP_ENDPOINT")
        if not endpoint:
            logger.warning(
                "DSE_OTEL_EXPORTER=otlp but DSE_OTEL_EXPORTER_OTLP_ENDPOINT is not set; "
                "falling back to ConsoleSpanExporter (local mode)."
            )
            return ConsoleSpanExporter()
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
        except ImportError:
            logger.warning(
                "opentelemetry-exporter-otlp-proto-grpc is not installed; "
                "falling back to ConsoleSpanExporter. Install it for production."
            )
            return ConsoleSpanExporter()
        return OTLPSpanExporter(endpoint=endpoint, insecure=True)
    return ConsoleSpanExporter()


def setup_tracing() -> TracingInterceptor:
    """Configure the global TracerProvider (idempotent) and return the
    interceptor ready for `Worker(..., interceptors=[...])`."""
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider(
            resource=Resource.create({"service.name": _SERVICE_NAME})
        )
        provider.add_span_processor(BatchSpanProcessor(_build_exporter()))
        trace.set_tracer_provider(provider)
    return TracingInterceptor(tracer=trace.get_tracer(_SERVICE_NAME))
