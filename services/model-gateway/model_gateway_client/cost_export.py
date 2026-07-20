"""Agregação de custo por tenant/task-class/stage (WSD-E3-T2 / WSD-E3-T4).

Fonte DURÁVEL (default a partir da Fase 2 — WSD-E3-T4): a tabela
`model_call_ledger` (`ledger.py`), gravada a cada chamada bem-sucedida com o
custo/tokens REAIS do LiteLLM. Sobrevive a restart do processo e agrega entre
processos — a pendência #4 do adendo ("hoje em memória por processo") está
resolvida. `aggregate_cost(..., source="ledger")` (default) lê daí.

Fonte em memória (legado da Fase 1, ainda disponível via `source="memory"`):
os spans do `InMemorySpanExporter` do processo atual
(`telemetry.get_recorded_spans()`). Útil para testes de unidade puros que não
querem tocar o Postgres.

Os spans OTel continuam sendo exportados em paralelo para o collector do WS-F
quando `DSE_OTEL_EXPORTER_OTLP_ENDPOINT` está setado (dashboards/alerting do
WSF-E7). O collector local só faz `debug`/stdout (sem backend consultável neste
ambiente), então a agregação consultável mora no ledger Postgres, não no
collector — ver README §"WSD-E3-T4".
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
    """Buffer em memória do processo atual (source="memory"). Fonte legado da
    Fase 1 — ver docstring do módulo."""
    return telemetry.get_recorded_spans()


def aggregate_cost(*, tenant_id: str | None = None, source: str = "ledger") -> list[dict]:
    """Agrega custo/tokens por (tenant_id, task_class, stage). Se
    `tenant_id` for passado, filtra só aquele tenant (isolamento — nunca
    devolve dados de outro tenant misturados por engano).

    `source`:
      - "ledger" (default, WSD-E3-T4): tabela durável `model_call_ledger`;
      - "memory": spans do processo atual (legado Fase 1)."""
    if source == "ledger":
        return ledger.aggregate(tenant_id=tenant_id)
    if source != "memory":
        raise ValueError(f"source inválido: {source!r} (use 'ledger' ou 'memory')")

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
