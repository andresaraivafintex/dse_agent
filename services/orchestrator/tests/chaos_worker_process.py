"""Processo de worker usado SOMENTE pelo chaos test (`test_chaos.py`).

Roda como um SUBPROCESSO de verdade (nao uma coroutine no mesmo processo do
teste) para que possamos mata-lo com SIGKILL de fora e simular a queda real
de um worker Temporal no meio de uma Activity longa (WSB-E5-T3).

Uso:
    python chaos_worker_process.py <temporal_address> <task_queue> <mode> <sentinel_dir>

`mode`:
  - "hang":   `run_coder_turn` escreve um arquivo sentinela `started` assim
              que comeca, depois dorme por uma "eternidade" (nunca manda
              heartbeat) — o teste mata este processo enquanto ele dorme.
  - "normal": `run_coder_turn` completa rapido normalmente — usado para o
              segundo worker (o que "assume" apos a queda do primeiro).
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
        # Substitui a fake `run_coder_turn` por uma versao que avisa via
        # sentinela e depois trava (sem heartbeat) ate ser morta.
        async def hanging_run_coder_turn(payload: dict) -> CoderTurnResult:
            (sentinel_path / "started").write_text("1")
            await asyncio.sleep(3600)  # nunca completa nesta execucao
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
        await asyncio.Event().wait()  # roda ate ser morto/terminado externamente


if __name__ == "__main__":
    asyncio.run(main())
