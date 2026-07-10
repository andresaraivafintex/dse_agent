"""WSB-E1-T4 — propagacao de trace OpenTelemetry de workflow para Activity.

Em vez de reinventar a propagacao de contexto (frágil dentro do sandbox
deterministico do workflow), usamos o interceptor oficial
`temporalio.contrib.opentelemetry.TracingInterceptor`, que já resolve:
  - criacao de span por workflow run / activity execution;
  - propagacao do trace context via `headers` do Temporal (o mesmo canal
    usado para outros metadados cross-boundary);
  - compatibilidade com o sandbox do workflow (nao chama APIs
    nao-deterministicas dentro do `@workflow.run`).

`setup_tracing()` monta um `TracerProvider` real (opentelemetry-sdk) com
exporter configuravel por env:
  - `DSE_OTEL_EXPORTER=console` (default, modo local/dev sem infra de
    observabilidade real) — imprime spans no stdout do worker;
  - `DSE_OTEL_EXPORTER=otlp` + `DSE_OTEL_EXPORTER_OTLP_ENDPOINT=<host:porta>`
    — para producao, quando um collector OTLP real (ex.: o da WS-F) existir.
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
                "DSE_OTEL_EXPORTER=otlp mas DSE_OTEL_EXPORTER_OTLP_ENDPOINT nao definido; "
                "caindo para ConsoleSpanExporter (modo local)."
            )
            return ConsoleSpanExporter()
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
        except ImportError:
            logger.warning(
                "opentelemetry-exporter-otlp-proto-grpc nao instalado; "
                "caindo para ConsoleSpanExporter. Instale-o para producao."
            )
            return ConsoleSpanExporter()
        return OTLPSpanExporter(endpoint=endpoint, insecure=True)
    return ConsoleSpanExporter()


def setup_tracing() -> TracingInterceptor:
    """Configura o TracerProvider global (idempotente) e retorna o
    interceptor pronto para `Worker(..., interceptors=[...])`."""
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider(
            resource=Resource.create({"service.name": _SERVICE_NAME})
        )
        provider.add_span_processor(BatchSpanProcessor(_build_exporter()))
        trace.set_tracer_provider(provider)
    return TracingInterceptor(tracer=trace.get_tracer(_SERVICE_NAME))
