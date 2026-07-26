"""Running long synchronous calls with real Temporal heartbeats.

Agent SDKs currently expose a synchronous surface. Calling them directly inside
an ``async`` Activity blocks the event loop and prevents heartbeats. This helper
offloads only the blocking call to a thread and keeps the Activity's loop free
to emit periodic progress.

Outside a Temporal Activity the same helper keeps working and never tries to
reach the non-existent ``temporalio.activity`` context.
"""
from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from typing import Any, TypeVar

from temporalio import activity


HEARTBEAT_INTERVAL_ENV_VAR = "DSE_ACTIVITY_HEARTBEAT_INTERVAL_SECONDS"
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 20.0
_MIN_HEARTBEAT_INTERVAL_SECONDS = 0.01

_T = TypeVar("_T")


def _configured_interval(override: float | None = None) -> float:
    raw: str | float = (
        override
        if override is not None
        else os.environ.get(HEARTBEAT_INTERVAL_ENV_VAR, str(DEFAULT_HEARTBEAT_INTERVAL_SECONDS))
    )
    try:
        interval = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{HEARTBEAT_INTERVAL_ENV_VAR}={raw!r} must be a positive number"
        ) from exc
    if interval < _MIN_HEARTBEAT_INTERVAL_SECONDS:
        raise ValueError(
            f"{HEARTBEAT_INTERVAL_ENV_VAR} must be >= {_MIN_HEARTBEAT_INTERVAL_SECONDS}s"
        )
    return interval


def _effective_interval(configured: float) -> float:
    """Keep at least three chances to beat within the heartbeat timeout."""
    if not activity.in_activity():
        return configured
    timeout = activity.info().heartbeat_timeout
    if timeout is None:
        return configured
    timeout_seconds = timeout.total_seconds()
    if timeout_seconds <= 0:
        return configured
    return max(
        _MIN_HEARTBEAT_INTERVAL_SECONDS,
        min(configured, timeout_seconds / 3.0),
    )


def _details(
    *,
    stage: str,
    work_item_id: str,
    operation: str,
    state: str,
    started_at: float,
    sequence: int,
) -> dict[str, Any]:
    # Deliberately no instruction, prompt, virtual key or model output: the
    # heartbeat goes into Temporal's operational history.
    return {
        "schema_version": 1,
        "component": "sandbox-runtime",
        "stage": stage,
        "work_item_id": work_item_id,
        "operation": operation,
        "state": state,
        "sequence": sequence,
        "elapsed_seconds": round(max(0.0, time.monotonic() - started_at), 3),
    }


async def run_sync_with_heartbeat(
    fn: Callable[..., _T],
    /,
    *args: Any,
    stage: str,
    work_item_id: str,
    operation: str,
    interval_seconds: float | None = None,
    **kwargs: Any,
) -> _T:
    """Run ``fn`` without blocking the Activity's heartbeats.

    The first beat is emitted before the call, the following ones at a regular
    interval, and a final one after it returns. Exceptions from the function or
    from the heartbeat are not swallowed. When called outside Temporal, it
    simply runs ``fn`` on the worker thread and returns/propagates the same
    result.
    """
    configured_interval = _configured_interval(interval_seconds)
    in_activity = activity.in_activity()
    interval = _effective_interval(configured_interval) if in_activity else configured_interval
    started_at = time.monotonic()
    sequence = 0

    if in_activity:
        activity.heartbeat(
            _details(
                stage=stage,
                work_item_id=work_item_id,
                operation=operation,
                state="started",
                started_at=started_at,
                sequence=sequence,
            )
        )

    call = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
    while not call.done():
        done, _ = await asyncio.wait({call}, timeout=interval)
        if call in done:
            break
        if in_activity:
            sequence += 1
            activity.heartbeat(
                _details(
                    stage=stage,
                    work_item_id=work_item_id,
                    operation=operation,
                    state="running",
                    started_at=started_at,
                    sequence=sequence,
                )
            )

    result = await call
    if in_activity:
        sequence += 1
        activity.heartbeat(
            _details(
                stage=stage,
                work_item_id=work_item_id,
                operation=operation,
                state="completed",
                started_at=started_at,
                sequence=sequence,
            )
        )
    return result

