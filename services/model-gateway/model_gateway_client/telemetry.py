"""Instrumentação OTel do model-gateway client (WSD-E3-T1/T2).

Cada chamada de modelo feita via `gateway_call.chat_completion` gera um span
`dse.model_gateway.chat_completion` com os atributos definidos em
`dse_contracts.constants` (contrato estável entre WS-D e WS-F):
`OTEL_ATTR_TENANT/WORK_ITEM/STAGE/MODEL/COST_USD/TOKENS_IN/TOKENS_OUT`,
preenchidos a partir da resposta real do LiteLLM (ele já retorna
custo/tokens — não recalculamos nada aqui).

Dois destinos de exportação, sempre ativos ao mesmo tempo:
  1. Um `InMemorySpanExporter` — sempre ligado. É o que permite testar
     `WSD-E3-T2` (agregação de custo) e os próprios testes pytest sem
     depender de nenhum collector rodando. Acesse via `get_recorded_spans()`.
  2. Um exportador OTLP/HTTP real para o collector do WS-F, SE
     `DSE_OTEL_EXPORTER_OTLP_ENDPOINT` estiver setado. Nesta sessão não há
     collector do WS-F no ar (services/platform/ ainda não publicou um) —
     documentamos a integração esperada mas ela é condicional/opt-in e não
     quebra nada se ausente.
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

# Atributo extra, fora do contrato publicado em dse_contracts.constants (que
# não define um OTEL_ATTR_TASK_CLASS hoje) — mas necessário para a agregação
# de custo por tenant/task-class/stage pedida em WSD-E3-T2. Segue a mesma
# convenção de nome (`dse.<campo>`) e é aditivo: não substitui nenhum
# atributo do contrato, só complementa. Documentado no README como um campo
# que deveria subir para o contrato compartilhado quando WS-F tocar nele.
OTEL_ATTR_TASK_CLASS = "dse.task_class"

_IN_MEMORY_EXPORTER = InMemorySpanExporter()
_PROVIDER: TracerProvider | None = None


def _build_provider() -> TracerProvider:
    provider = TracerProvider(
        resource=Resource.create({"service.name": "dse-model-gateway-client"})
    )
    # Sempre ativo — é o backend usado pelos testes e pelo cost_export local.
    provider.add_span_processor(SimpleSpanProcessor(_IN_MEMORY_EXPORTER))

    endpoint = settings.otlp_exporter_endpoint()
    if endpoint:
        # Caminho de produção esperado: OTel collector do WS-F. Import local
        # para não forçar a dependência de rede em quem não configurou nada.
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
    """Todos os spans emitidos pelo processo atual — usado por testes e por
    `cost_export.py`. Em produção, a fonte de verdade é o backend do
    collector (Tempo/Jaeger/etc. do WS-F), não este buffer em memória."""
    get_tracer_provider()  # garante que o provider (e o processor) existe
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
    """Span de uma chamada de modelo. Atributos de custo/tokens são setados
    pelo caller (`gateway_call.chat_completion`) depois que a resposta do
    LiteLLM chega — só ele sabe o custo real daquela chamada específica."""
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
