"""Entrypoint do processo dispatcher (`python -m ingest_gateway.dispatcher_main`).
Processo separado do FastAPI app — roda `Dispatcher.run_forever()` contra o
Temporal real (`TEMPORAL_ADDRESS`, default `localhost:7233`)."""
from __future__ import annotations

import asyncio
import os

from dse_contracts import TASK_QUEUE
from temporalio.client import Client

from .dispatcher import Dispatcher


async def main() -> None:
    address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    client = await Client.connect(address)
    dispatcher = Dispatcher(client)
    print(f"[ingest-gateway] dispatcher running against {address}, task_queue={TASK_QUEUE}")
    await dispatcher.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
