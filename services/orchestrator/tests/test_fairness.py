"""WSB-E1-T3 — per-tenant fairness, worker-side.

Levels of proof (all REAL, nothing mocked of what is under test):
  1. A real concurrency BURST test (asyncio + wall clock): a tenant saturating
     its own cap does NOT push the other tenant's dispatch time beyond the SLO.
     That is the whole point of fairness.
  2. The Postgres CapProvider really reads `tenant_config` (WS-F, migration
     0007), with the documented precedence.
  3. End-to-end integration: the `FairnessInterceptor` on a real Temporal Worker
     (time-skipping) gates Activities per tenant without affecting the result.
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


# peak concurrency observed by `fairness_probe` (module-global so the test can
# see it, since the Activity runs in the SAME process as the in-proc worker).
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
    """Dispatch SLO: with per-tenant caps, tenant B gets a slot ALMOST
    immediately even with tenant A saturated and a huge queue of pending work.
    Without fairness (one global pool), B would sit behind A's entire queue and
    violate the SLO."""
    caps = {"tenant-A": 2, "tenant-B": 2}
    controller = WorkerSideFairnessController(lambda t: caps.get(t, 1))

    HOLD = 0.5           # each "activity" holds the slot for 0.5s
    SLO_SECONDS = 0.25   # B must get a slot well before A drains

    async def busy(tenant: str, hold: float = HOLD):
        async with controller.acquire(tenant):
            await asyncio.sleep(hold)

    # tenant A saturates its cap (2) and queues MUCH more (20) — a storm.
    a_tasks = [asyncio.create_task(busy("tenant-A")) for _ in range(22)]
    # let A take ownership of its 2 slots
    await asyncio.sleep(0.05)

    # now tenant B asks for a slot and we time how long it waits.
    started = time.monotonic()
    async with controller.acquire("tenant-B"):
        waited = time.monotonic() - started
        # B got a slot of its own: it did not wait for A's queue to drain
        assert waited < SLO_SECONDS, f"tenant B esperou {waited:.3f}s (> SLO {SLO_SECONDS}s)"
    # and A's wait got large (proof that A really WAS saturated)
    await asyncio.gather(*a_tasks)
    assert controller.max_wait_seconds.get("tenant-A", 0) > SLO_SECONDS


@pytest.mark.asyncio
async def test_postgres_cap_provider_reads_tenant_config_precedence():
    tid = f"fair-{uuid.uuid4().hex[:8]}"
    provider = postgres_cap_provider()

    # no row -> default (never unlimited)
    from dse_orchestrator.fairness import DEFAULT_TENANT_CAP
    assert provider(tid) == DEFAULT_TENANT_CAP

    # max_concurrent_work_items honored
    set_tenant_config(tid, max_concurrent_work_items=3)
    assert provider(tid) == 3

    # fairness->>'max_concurrent_activities' TAKES PRECEDENCE
    set_tenant_config(tid, max_concurrent_work_items=3, max_concurrent_activities=7)
    assert provider(tid) == 7


@pytest.mark.asyncio
async def test_fairness_interceptor_gates_activity_per_tenant_on_real_worker(time_skipping_env):
    """The interceptor on a real Temporal Worker limits per-tenant Activity
    concurrency to the cap, without changing the Activity result (fairness is
    transparent to the workflow)."""
    task_queue = f"tq-fair-{uuid.uuid4().hex[:8]}"
    _PEAK["n"] = 0
    _PEAK["cur"] = 0

    controller = WorkerSideFairnessController(lambda t: 2)  # cap of 2 for the tenant
    interceptor = FairnessInterceptor(controller)

    async with Worker(
        time_skipping_env.client, task_queue=task_queue,
        workflows=[_FanOutWorkflow], activities=[fairness_probe],
        interceptors=[interceptor], max_concurrent_activities=50,
    ):
        # one workflow fires 6 activities of the SAME tenant in parallel; cap=2
        # must keep the Activity concurrency peak at 2.
        results = await time_skipping_env.client.execute_workflow(
            _FanOutWorkflow.run, 6, id=f"fanout-{uuid.uuid4().hex[:8]}", task_queue=task_queue,
        )

    assert sorted(results) == [0, 1, 2, 3, 4, 5]
    assert _PEAK["n"] <= 2, f"pico de concorrencia {_PEAK['n']} excedeu o cap 2"
