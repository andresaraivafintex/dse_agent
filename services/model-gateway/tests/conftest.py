"""Fixtures compartilhadas dos testes do WS-D.

Estes testes rodam contra a infra REAL (Postgres em localhost:5432, Vault
dev em localhost:8200, e o LiteLLM proxy + modelo eco subidos via
`docker-compose.wsd.yml`, ver README "Como rodar os testes"). Nada aqui é
mockado — a garantia que estamos provando (virtual key emitida de verdade,
revogada de verdade, linha de audit gravada de verdade) é o próprio ponto do
sistema (P8).
"""
from __future__ import annotations

import os
import uuid

import pytest

# Defaults que casam com docker-compose.wsd.yml / docker-compose.yml da
# fundação — só aplicados se a env var ainda não estiver setada, então
# quem quiser rodar contra outra infra (CI, outra máquina) pode sobrescrever.
os.environ.setdefault("DSE_MODEL_GATEWAY_BASE_URL", "http://localhost:4000")
os.environ.setdefault("DSE_LITELLM_MASTER_KEY", "sk-dse-local-dev-master-key")
os.environ.setdefault("VAULT_ADDR", "http://localhost:8200")
os.environ.setdefault("VAULT_TOKEN", "dse_dev_root")
os.environ.setdefault("DSE_DATABASE_URL", "postgresql://dse:dse_dev_only@localhost:5432/dse")
os.environ.setdefault(
    "DSE_AUDIT_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
)

ECHO_MODEL = "eco/echo-model"


@pytest.fixture
def unique_ids():
    """IDs únicos por teste — os testes rodam contra Postgres/LiteLLM reais
    e compartilhados, então nunca reusar tenant/work_item fixos evita que um
    teste veja resíduo de outro."""
    suffix = uuid.uuid4().hex[:10]
    return {
        "tenant_id": f"tenant-test-{suffix}",
        "work_item_id": f"wi-test-{suffix}",
    }


@pytest.fixture(autouse=True)
def _clear_span_recorder():
    """Cada teste começa com o recorder de spans em memória limpo (evita
    contaminação entre testes de telemetry/cost_export, que leem o buffer
    inteiro do processo)."""
    from model_gateway_client import telemetry

    telemetry.clear_recorded_spans()
    yield
    telemetry.clear_recorded_spans()
