"""Worker process used ONLY by the chaos test (`test_chaos.py`).

Runs as a real SUBPROCESS (not a coroutine in the test's own process) so we can
SIGKILL it from the outside and simulate a real Temporal worker crash in the
middle of a long Activity (WSB-E5-T3).

Usage:
    python chaos_worker_process.py <temporal_address> <task_queue> <mode> <sentinel_dir>

`mode`:
  - "hang":   `run_coder_turn` writes a `started` sentinel file as soon as it
              begins, then sleeps for an "eternity" (never heartbeats) — the
              test kills this process while it sleeps.
  - "normal": `run_coder_turn` completes quickly as usual — used for the second
              worker (the one that "takes over" after the first one crashes).
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeControlPlane, build_fake_activities  # noqa: E402

from dse_contracts.activities import ACTIVITY_RUN_CODER_TURN, CoderTurnResult  # noqa: E402
from dse_orchestrator.local_activities import LOCAL_ACTIVITIES  # noqa: E402
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow  # noqa: E402
from temporalio import activity  # noqa: E402


def _activity_name(fn) -> str | None:
    defn = activity._Definition.from_callable(fn)
    return defn.name if defn else None


async def main() -> None:
    temporal_address, task_queue, mode, sentinel_dir = sys.argv[1:5]
    sentinel_path = Path(sentinel_dir)
    sentinel_path.mkdir(parents=True, exist_ok=True)

    state = FakeControlPlane()
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    if mode == "hang":
        # Replaces the `run_coder_turn` fake with a version that signals via a
        # sentinel and then hangs (no heartbeat) until it is killed.
        async def hanging_run_coder_turn(payload: dict) -> CoderTurnResult:
            (sentinel_path / "started").write_text("1")
            await asyncio.sleep(3600)  # never completes in this run
            return CoderTurnResult(sandbox_id=payload["sandbox_id"], diff_summary="unreachable")

        activities = [a for a in activities if _activity_name(a) != ACTIVITY_RUN_CODER_TURN]
        activities.append(activity.defn(name=ACTIVITY_RUN_CODER_TURN)(hanging_run_coder_turn))

    client = await Client.connect(temporal_address, data_converter=pydantic_data_converter)
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow],
        activities=activities,
        identity=f"chaos-worker-{mode}-{os.getpid()}",
    )
    (sentinel_path / f"worker-up-{mode}").write_text(str(os.getpid()))
    async with worker:
        await asyncio.Event().wait()  # runs until killed/terminated externally


if __name__ == "__main__":
    asyncio.run(main())
