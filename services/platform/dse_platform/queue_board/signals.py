"""WSF-E6-T2 — sending Temporal signals from the queue board.

The board does not talk to Temporal directly inside the handlers; it goes
through the `SignalSender` interface. Two implementations:
  - `TemporalSignalSender`: the real one, uses `temporalio.client.Client` to
    connect to the cluster (`localhost:7233`) and call `handle.signal(name, arg)`
    on the workflow whose `workflow_id == work_item_id` (foundation contract:
    StartWorkflow(workflow_id = work_item_id)).
  - `FakeSignalSender`: records the calls in memory — used in the operator tests
    (the audit path is the real one; only the signal transport is fake, so no
    live workflow is needed per test). CLEARLY MARKED.

The signal names match the `@workflow.signal` handlers in
`services/orchestrator/.../workflows.py` (WS-B): pause, resume, cancel,
retry_from_checkpoint, force_clarification, escalate, reassign_model,
reassign_runtime. Names are centralized here as constants.
"""
from __future__ import annotations

import abc

SIGNAL_PAUSE = "pause"
SIGNAL_RESUME = "resume"
SIGNAL_CANCEL = "cancel"
SIGNAL_RETRY_FROM_CHECKPOINT = "retry_from_checkpoint"
SIGNAL_FORCE_CLARIFICATION = "force_clarification"
SIGNAL_ESCALATE = "escalate"
SIGNAL_REASSIGN_MODEL = "reassign_model"
SIGNAL_REASSIGN_RUNTIME = "reassign_runtime"

VALID_SIGNALS = {
    SIGNAL_PAUSE, SIGNAL_RESUME, SIGNAL_CANCEL, SIGNAL_RETRY_FROM_CHECKPOINT,
    SIGNAL_FORCE_CLARIFICATION, SIGNAL_ESCALATE, SIGNAL_REASSIGN_MODEL, SIGNAL_REASSIGN_RUNTIME,
}


class SignalSender(abc.ABC):
    @abc.abstractmethod
    def signal(self, workflow_id: str, signal_name: str, arg=None) -> None:
        ...


class TemporalSignalSender(SignalSender):
    """The real one. Connects on demand (one connection per sender, reused).
    Imports `temporalio` lazily so the dependency is not required in environments
    that only use the FakeSignalSender."""

    def __init__(self, target: str = "localhost:7233", namespace: str = "default"):
        self._target = target
        self._namespace = namespace
        self._client = None

    async def _client_async(self):
        from temporalio.client import Client

        if self._client is None:
            self._client = await Client.connect(self._target, namespace=self._namespace)
        return self._client

    def signal(self, workflow_id: str, signal_name: str, arg=None) -> None:
        if signal_name not in VALID_SIGNALS:
            raise ValueError(f"unknown signal: {signal_name!r}")
        import asyncio

        async def _do():
            client = await self._client_async()
            handle = client.get_workflow_handle(workflow_id)
            if arg is None:
                await handle.signal(signal_name)
            else:
                await handle.signal(signal_name, arg)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            # called from inside an async loop (e.g. an async FastAPI handler):
            # the caller must use the async variant instead.
            raise RuntimeError("use signal_async inside a running event loop")
        asyncio.run(_do())

    async def signal_async(self, workflow_id: str, signal_name: str, arg=None) -> None:
        if signal_name not in VALID_SIGNALS:
            raise ValueError(f"unknown signal: {signal_name!r}")
        client = await self._client_async()
        handle = client.get_workflow_handle(workflow_id)
        if arg is None:
            await handle.signal(signal_name)
        else:
            await handle.signal(signal_name, arg)


class FakeSignalSender(SignalSender):
    """Test FIXTURE (marked). Records signal calls in `.sent` instead of sending
    them to Temporal — so the operator path (validation + audit) can be exercised
    without a live workflow. `raises_for` simulates a signal that fails (e.g. a
    nonexistent workflow) to test the fail-closed behavior."""

    def __init__(self, raises_for: set[str] | None = None):
        self.sent: list[tuple[str, str, object]] = []
        self._raises_for = raises_for or set()

    def signal(self, workflow_id: str, signal_name: str, arg=None) -> None:
        if signal_name not in VALID_SIGNALS:
            raise ValueError(f"unknown signal: {signal_name!r}")
        if workflow_id in self._raises_for:
            raise RuntimeError(f"workflow {workflow_id} not found (simulated)")
        self.sent.append((workflow_id, signal_name, arg))
