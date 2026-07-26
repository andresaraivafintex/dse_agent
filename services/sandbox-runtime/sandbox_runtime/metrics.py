"""OTel metrics for sandbox runtime per resource class (WSC-E1-T2).

Uses the attribute names defined in `dse_contracts.constants.OTEL_ATTR_*` so
that WSF-E7 (dashboards/alerting) can correlate with what WSD-E3
(model-gateway) emits, without needing real-time coordination.

In production the exporter is configured through the OTel SDK's standard
environment variables (`OTEL_EXPORTER_OTLP_ENDPOINT` etc., see
opentelemetry.exporter.otlp) — this module does not force any specific
exporter; if none is configured it uses an `InMemoryMetricReader`-friendly
`MeterProvider` (not actually exporting) so it does not break processes that
have no collector running.
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
    # Boring-first (P7): with no collector configured, export to the console (dev);
    # `readers=[]` would be too silent to prove the metric exists at all — we
    # prefer a real, cheap reader (console) as the dev default.
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
    """Used exclusively by the test suite to inject a `MeterProvider` with an
    `InMemoryMetricReader` so tests can assert on the emitted data points (see
    tests/test_resource_caps_and_metrics.py). Never called from production
    code."""
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
    """Emit an OTel data point with the runtime minutes consumed by a sandbox,
    attributed to a resource class (e.g. "small"/"medium"/"large", derived from
    the WorkItem budget). Called by `teardown_sandbox`."""
    _histogram().record(
        minutes,
        attributes={
            OTEL_ATTR_TENANT: tenant_id,
            OTEL_ATTR_WORK_ITEM: work_item_id,
            OTEL_ATTR_STAGE: stage,
            "dse.resource_class": resource_class,
        },
    )
