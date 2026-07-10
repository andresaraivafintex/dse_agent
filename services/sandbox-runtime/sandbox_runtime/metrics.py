"""Métricas OTel de runtime de sandbox por resource class (WSC-E1-T2).

Usa os nomes de atributo definidos em `dse_contracts.constants.OTEL_ATTR_*`
para que WSF-E7 (dashboards/alerting) consiga correlacionar com o que
WSD-E3 (model-gateway) emite, sem precisar coordenar em tempo real.

Em produção, o exporter é configurado via variáveis de ambiente padrão do
OTel SDK (`OTEL_EXPORTER_OTLP_ENDPOINT` etc., ver
opentelemetry.exporter.otlp) — este módulo não força nenhum exporter
específico; se nenhum estiver configurado, usa um
`InMemoryMetricReader`-friendly `MeterProvider` (sem exportar de verdade)
para não quebrar processos que não têm um collector no ar.
"""
from __future__ import annotations

import os

from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    PeriodicExportingMetricReader,
)

from dse_contracts.constants import (
    OTEL_ATTR_STAGE,
    OTEL_ATTR_TENANT,
    OTEL_ATTR_WORK_ITEM,
)

_METER_NAME = "dse.sandbox_runtime"
_provider: MeterProvider | None = None


def _get_provider() -> MeterProvider:
    global _provider
    if _provider is not None:
        return _provider
    # Boring-first (P7): sem collector configurado, exporta pro console (dev);
    # `readers=[]` seria silencioso demais para provar que a métrica existe —
    # preferimos um reader real e barato (console) por padrão em dev.
    if os.environ.get("DSE_OTEL_DISABLE_CONSOLE_EXPORT") == "1":
        _provider = MeterProvider()
    else:
        reader = PeriodicExportingMetricReader(
            ConsoleMetricExporter(), export_interval_millis=60_000
        )
        _provider = MeterProvider(metric_readers=[reader])
    metrics.set_meter_provider(_provider)
    return _provider


def get_meter():
    return _get_provider().get_meter(_METER_NAME)


def set_provider_for_test(provider: MeterProvider) -> None:
    """Usado exclusivamente pela suíte de testes para injetar um
    `MeterProvider` com um `InMemoryMetricReader` e conseguir fazer asserts
    sobre os data points emitidos (ver tests/test_resource_caps_and_metrics.py).
    Nunca chamado em código de produção."""
    global _provider, _runtime_histogram
    _provider = provider
    metrics.set_meter_provider(provider)
    _runtime_histogram = None


_runtime_histogram = None


def _histogram():
    global _runtime_histogram
    if _runtime_histogram is None:
        _runtime_histogram = get_meter().create_histogram(
            name="dse.sandbox.runtime_minutes",
            unit="min",
            description="Minutos de runtime de um sandbox por resource class",
        )
    return _runtime_histogram


def record_sandbox_runtime_minutes(
    *,
    tenant_id: str,
    work_item_id: str,
    stage: str,
    resource_class: str,
    minutes: float,
) -> None:
    """Emite um data point OTel de minutos de runtime consumidos por um
    sandbox, atribuído a uma resource class (ex.: "small"/"medium"/"large",
    derivada do budget do WorkItem). Chamado por `teardown_sandbox`."""
    _histogram().record(
        minutes,
        attributes={
            OTEL_ATTR_TENANT: tenant_id,
            OTEL_ATTR_WORK_ITEM: work_item_id,
            OTEL_ATTR_STAGE: stage,
            "dse.resource_class": resource_class,
        },
    )
