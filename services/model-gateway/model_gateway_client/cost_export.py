"""Agregação de custo por tenant/task-class/stage (WSD-E3-T2).

Fonte de dados nesta Fase 1: os spans em memória gravados por
`telemetry.get_recorded_spans()` (o mesmo `InMemorySpanExporter` que os
testes usam) — suficiente para provar a lógica de agregação localmente sem
nenhuma infra extra.

Integração real esperada em produção (documentada, não implementada aqui —
depende do OTel collector do WS-F, que ainda não existe neste momento):
um collector recebe os spans via OTLP (ver
`settings.otlp_exporter_endpoint()` / `DSE_OTEL_EXPORTER_OTLP_ENDPOINT`),
grava em um backend com suporte a agregação (Tempo+metrics-generator,
ClickHouse, ou simplesmente um exporter de métricas derivadas via
span-metrics connector). Este módulo deveria então virar uma query contra
esse backend em vez de ler o buffer em memória do processo atual — a forma
da função de agregação (`aggregate_cost`) já é a interface estável para essa
troca: troque só `_iter_spans()`.
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

from . import telemetry
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
    """Ponto de troca para produção: em vez do buffer em memória, faria uma
    query no backend do collector do WS-F. Ver docstring do módulo."""
    return telemetry.get_recorded_spans()


def aggregate_cost(*, tenant_id: str | None = None) -> list[dict]:
    """Agrega custo/tokens por (tenant_id, task_class, stage). Se
    `tenant_id` for passado, filtra só aquele tenant (isolamento — nunca
    devolve dados de outro tenant misturados por engano)."""
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
    """Atalho: total de custo por tenant (soma de todas as task_class/stage)."""
    totals: dict[str, float] = defaultdict(float)
    for row in aggregate_cost():
        totals[row["tenant_id"]] += row["total_cost_usd"]
    return dict(totals)
