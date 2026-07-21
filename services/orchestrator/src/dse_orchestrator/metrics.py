"""Fase 3 — ativacao do alerta de aproximacao do limite de history do Temporal
(infra/ALERTING-RULES.md §3, em conjunto com o WS-F).

O servidor Temporal emite `temporal_workflow_event_history_size` nativamente,
mas o compose da fundacao ainda nao expoe as metricas Prometheus do servidor
(TODO explicito da regra §3). Enquanto isso, o proprio orquestrador emite uma
metrica OTel com o tamanho APROXIMADO do run atual, medido do lado do worker:

  - `dse.workflow.history_length`  — `workflow.info().get_current_history_length()`
    (contagem real de eventos do run atual, exposta pelo SDK de forma
    deterministica/replay-safe);
  - `dse.workflow.history_size_bytes` — `get_current_history_size()` (bytes);
  - `dse.workflow.continue_as_new_count` — quantas vezes esta cadeia de
    execucoes ja fez Continue-As-New (o run atual "zera" o history a cada CAN;
    a contagem da o contexto de quantos resets ja houve).

Atributos: `dse.work_item_id`, `dse.tenant_id`, `dse.stage` (fase do workflow)
e `dse.checkpoint` (fronteira que emitiu). O WS-F aponta a regra de alerta
(Warning 70% / Critical 90% de ~10k eventos) para `dse.workflow.history_length`.

A LEITURA acontece dentro do workflow (deterministica); a EMISSAO acontece na
Activity local `emit_history_metric` (I/O fora do sandbox — disciplina P1).

Exporter configuravel pelo MESMO env do tracing (`DSE_OTEL_EXPORTER`):
console (default local) ou otlp + `DSE_OTEL_EXPORTER_OTLP_ENDPOINT` apontando
para o otel-collector do WS-F (`otel-collector:4317` no compose).
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

_lock = threading.Lock()
_meter = None
_hist_length = None
_hist_size = None
_can_count = None


def _build_metric_reader():
    from opentelemetry.sdk.metrics.export import (
        ConsoleMetricExporter,
        MetricExportResult,
        PeriodicExportingMetricReader,
    )

    class _QuietConsoleMetricExporter(ConsoleMetricExporter):
        """Console exporter que tolera stdout ja fechado (ex.: flush de
        shutdown depois que o pytest fechou o capture) — metrica e sempre
        best-effort, nunca barulho fatal."""

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
                "DSE_OTEL_EXPORTER=otlp mas DSE_OTEL_EXPORTER_OTLP_ENDPOINT nao definido; "
                "metricas caem para ConsoleMetricExporter (modo local)."
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
                    "opentelemetry-exporter-otlp-proto-grpc nao instalado; metricas "
                    "caem para ConsoleMetricExporter. Instale-o para producao."
                )
    return PeriodicExportingMetricReader(
        _QuietConsoleMetricExporter(), export_interval_millis=60_000
    )


def _make_instruments() -> None:
    global _hist_length, _hist_size, _can_count
    _hist_length = _meter.create_histogram(
        METRIC_HISTORY_LENGTH,
        unit="{event}",
        description="Eventos no history do run ATUAL do WorkItemLifecycleWorkflow "
        "(get_current_history_length) — regra §3 de ALERTING-RULES.md",
    )
    _hist_size = _meter.create_histogram(
        METRIC_HISTORY_SIZE_BYTES,
        unit="By",
        description="Bytes do history do run atual (get_current_history_size)",
    )
    _can_count = _meter.create_histogram(
        METRIC_CONTINUE_AS_NEW,
        unit="{run}",
        description="Quantos Continue-As-New esta cadeia de execucoes ja fez",
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
    """Injeta um MetricReader (ex.: InMemoryMetricReader) — uso exclusivo de
    teste; substitui qualquer configuracao anterior deste modulo."""
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
