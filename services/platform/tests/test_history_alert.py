"""Ativação da ALERTING-RULES.md §3 — alerta de aproximação do limite de
history do Temporal, provado contra o otel-collector REAL da fundação
(dse_otel_collector, pipeline `metrics/history_alert` em
infra/otel-collector-config.yaml).

Mecânica do teste: envia datapoints OTLP/HTTP reais (o mesmo caminho que o
emit_history_metric do WS-B usa) acima e abaixo dos thresholds e verifica no
stdout do collector que SÓ os acima do threshold saíram no exporter
`debug/history_alert` (o canal do alerta no MVP), com a severidade correta.
"""
from __future__ import annotations

import subprocess
import time
import uuid

import pytest
import requests

OTLP_METRICS_URL = "http://localhost:4318/v1/metrics"
COLLECTOR_CONTAINER = "dse_otel_collector"

# Mesmos números do infra/otel-collector-config.yaml (70%/90% dos limites
# default do Temporal: 51.200 eventos / 50MB).
WARNING_EVENTS = 35_840
CRITICAL_EVENTS = 46_080


def _send_history_metric(metric_name: str, value: int, work_item_id: str) -> None:
    payload = {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "dse-orchestrator-test"}}
                    ]
                },
                "scopeMetrics": [
                    {
                        "scope": {"name": "dse.test.history_alert"},
                        "metrics": [
                            {
                                "name": metric_name,
                                "gauge": {
                                    "dataPoints": [
                                        {
                                            "asInt": str(value),
                                            "timeUnixNano": str(time.time_ns()),
                                            "attributes": [
                                                {"key": "dse.work_item_id", "value": {"stringValue": work_item_id}},
                                                {"key": "dse.tenant_id", "value": {"stringValue": "alert-test"}},
                                            ],
                                        }
                                    ]
                                },
                            }
                        ],
                    }
                ],
            }
        ]
    }
    resp = requests.post(OTLP_METRICS_URL, json=payload, timeout=10)
    assert resp.status_code == 200, f"OTLP HTTP {resp.status_code}: {resp.text}"


def _alert_channel_lines(since: str) -> list[str]:
    """Linhas do stdout do collector que pertencem a blocos do exporter
    debug/history_alert (o canal do alerta). O debug exporter loga um header
    com o nome do exporter e depois o dump do datapoint — um parser de
    estado simples separa os canais."""
    logs = subprocess.run(
        ["docker", "logs", COLLECTOR_CONTAINER, "--since", since],
        capture_output=True, text=True, timeout=30,
    )
    text = logs.stdout + logs.stderr
    lines, in_alert = [], False
    for line in text.splitlines():
        if '"kind": "exporter"' in line:
            in_alert = '"name": "debug/history_alert"' in line
            continue
        if in_alert:
            lines.append(line)
    return lines


@pytest.fixture(scope="module")
def collector():
    try:
        subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", COLLECTOR_CONTAINER],
            capture_output=True, text=True, check=True, timeout=15,
        )
        requests.get("http://localhost:4318", timeout=5)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"otel-collector da fundação indisponível: {exc}")
    return True


@pytest.mark.parametrize(
    "metric_name,value,expected_severity",
    [
        ("dse.workflow.history_length", WARNING_EVENTS + 200, "warning"),
        ("dse.workflow.history_length", CRITICAL_EVENTS + 200, "critical"),
        ("temporal_workflow_event_history_size", 47_500_000, "critical"),  # bytes: > 90% de 50MB
    ],
)
def test_above_threshold_fires_alert(collector, metric_name, value, expected_severity):
    wid = f"wi-alert-{uuid.uuid4().hex[:10]}"
    since = "5s"
    start = time.time()
    _send_history_metric(metric_name, value, wid)

    found = []
    while time.time() - start < 20:
        found = [ln for ln in _alert_channel_lines(f"{int(time.time() - start) + 6}s") if wid in ln]
        if found:
            break
        time.sleep(1)
    assert found, f"datapoint {value} de {metric_name} NÃO apareceu no canal de alerta ({since})"

    # a severidade marcada pelo transform está no mesmo bloco do datapoint
    window = _alert_channel_lines("60s")
    idx = next(i for i, ln in enumerate(window) if wid in ln)
    block = "\n".join(window[max(0, idx - 20): idx + 20])
    assert "dse.alert: Str(temporal_history_threshold_exceeded)" in block
    assert f"dse.alert_severity: Str({expected_severity})" in block


def test_below_threshold_does_not_fire(collector):
    wid = f"wi-quiet-{uuid.uuid4().hex[:10]}"
    _send_history_metric("dse.workflow.history_length", 5_000, wid)
    time.sleep(6)  # janela generosa para o pipeline (sem batch) processar

    assert not [ln for ln in _alert_channel_lines("30s") if wid in ln], (
        "datapoint ABAIXO do threshold vazou para o canal de alerta"
    )


def test_unrelated_metric_never_reaches_alert_channel(collector):
    wid = f"wi-other-{uuid.uuid4().hex[:10]}"
    _send_history_metric("dse.cost_usd_total", 999_999, wid)  # nem é métrica de history
    time.sleep(6)

    assert not [ln for ln in _alert_channel_lines("30s") if wid in ln], (
        "métrica não-history vazou para o canal de alerta"
    )
