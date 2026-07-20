"""WSB-E1-T3 — fairness por tenant, worker-side.

Dois niveis de prova (ambos REAIS, nada mockado do que esta sob teste):
  1. Teste de BURST de concorrencia real (asyncio + relogio de parede): um
     tenant saturando seu proprio cap NAO empurra o tempo de dispatch do outro
     tenant alem do SLO. E o proprio ponto da fairness.
  2. O CapProvider de Postgres le `tenant_config` (WS-F, migracao 0007) de
     verdade, com a precedencia documentada.
  3. Integracao ponta-a-ponta: o `FairnessInterceptor` num Worker Temporal real
     (time-skipping) gateia Activities por tenant sem afetar o resultado.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import timedelta

import pytest
from temporalio import activity, workflow
from temporalio.worker import Worker

from dse_orchestrator.fairness import (
    FairnessInterceptor,
    WorkerSideFairnessController,
    postgres_cap_provider,
)

from conftest import set_tenant_config


# pico de concorrencia observado pela `fairness_probe` (modulo-global para ser
# visivel ao teste, ja que a Activity roda no MESMO processo do worker in-proc).
_PEAK = {"n": 0, "cur": 0}
_PEAK_LOCK = asyncio.Lock()


@activity.defn(name="fairness_probe")
async def fairness_probe(payload: dict) -> int:
    async with _PEAK_LOCK:
        _PEAK["cur"] += 1
        _PEAK["n"] = max(_PEAK["n"], _PEAK["cur"])
    await asyncio.sleep(0.2)
    async with _PEAK_LOCK:
        _PEAK["cur"] -= 1
    return payload["i"]


@workflow.defn
class _FanOutWorkflow:
    @workflow.run
    async def run(self, n: int) -> list[int]:
        results = await asyncio.gather(*[
            workflow.execute_activity(
                "fairness_probe", {"i": i, "tenant_id": "solo-tenant"},
                start_to_close_timeout=timedelta(seconds=60),
            )
            for i in range(n)
        ])
        return list(results)


@pytest.mark.asyncio
async def test_burst_one_tenant_saturating_does_not_starve_another():
    """SLO de dispatch: com caps por tenant, o tenant B consegue um slot
    QUASE imediatamente mesmo com o tenant A saturado e com uma fila enorme de
    trabalho pendente. Sem fairness (um pool global), B ficaria atras de toda a
    fila de A e violaria o SLO."""
    caps = {"tenant-A": 2, "tenant-B": 2}
    controller = WorkerSideFairnessController(lambda t: caps.get(t, 1))

    HOLD = 0.5           # cada "activity" segura o slot por 0.5s
    SLO_SECONDS = 0.25   # B tem que conseguir um slot bem antes de A drenar

    async def busy(tenant: str, hold: float = HOLD):
        async with controller.acquire(tenant):
            await asyncio.sleep(hold)

    # tenant A satura seu cap (2) e enfileira MUITO mais (20) — uma tempestade.
    a_tasks = [asyncio.create_task(busy("tenant-A")) for _ in range(22)]
    # deixa A tomar posse dos seus 2 slots
    await asyncio.sleep(0.05)

    # agora o tenant B pede um slot e cronometramos quanto espera.
    started = time.monotonic()
    async with controller.acquire("tenant-B"):
        waited = time.monotonic() - started
        # B pegou um slot proprio: nao esperou a fila de A drenar
        assert waited < SLO_SECONDS, f"tenant B esperou {waited:.3f}s (> SLO {SLO_SECONDS}s)"
    # e a espera de A ficou grande (prova que A ESTAVA de fato saturado)
    await asyncio.gather(*a_tasks)
    assert controller.max_wait_seconds.get("tenant-A", 0) > SLO_SECONDS


@pytest.mark.asyncio
async def test_postgres_cap_provider_reads_tenant_config_precedence():
    tid = f"fair-{uuid.uuid4().hex[:8]}"
    provider = postgres_cap_provider()

    # sem linha -> default (nunca ilimitado)
    from dse_orchestrator.fairness import DEFAULT_TENANT_CAP
    assert provider(tid) == DEFAULT_TENANT_CAP

    # max_concurrent_work_items respeitado
    set_tenant_config(tid, max_concurrent_work_items=3)
    assert provider(tid) == 3

    # fairness->>'max_concurrent_activities' TEM PRECEDENCIA
    set_tenant_config(tid, max_concurrent_work_items=3, max_concurrent_activities=7)
    assert provider(tid) == 7


@pytest.mark.asyncio
async def test_fairness_interceptor_gates_activity_per_tenant_on_real_worker(time_skipping_env):
    """O interceptor num Worker Temporal real limita a concorrencia de Activity
    por tenant ao cap, sem alterar o resultado da Activity (fairness e
    transparente ao workflow)."""
    task_queue = f"tq-fair-{uuid.uuid4().hex[:8]}"
    _PEAK["n"] = 0
    _PEAK["cur"] = 0

    controller = WorkerSideFairnessController(lambda t: 2)  # cap 2 para o tenant
    interceptor = FairnessInterceptor(controller)

    async with Worker(
        time_skipping_env.client, task_queue=task_queue,
        workflows=[_FanOutWorkflow], activities=[fairness_probe],
        interceptors=[interceptor], max_concurrent_activities=50,
    ):
        # um workflow dispara 6 activities do MESMO tenant em paralelo; o cap=2
        # deve manter o pico de concorrencia de Activity em 2.
        results = await time_skipping_env.client.execute_workflow(
            _FanOutWorkflow.run, 6, id=f"fanout-{uuid.uuid4().hex[:8]}", task_queue=task_queue,
        )

    assert sorted(results) == [0, 1, 2, 3, 4, 5]
    assert _PEAK["n"] <= 2, f"pico de concorrencia {_PEAK['n']} excedeu o cap 2"
