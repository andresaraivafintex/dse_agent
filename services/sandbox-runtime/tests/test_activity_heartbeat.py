"""Heartbeat periódico real sem dependência de um servidor Temporal."""
from __future__ import annotations

import asyncio
import time

import pytest
from temporalio import activity
from temporalio.testing import ActivityEnvironment

from sandbox_runtime.activity_heartbeat import (
    HEARTBEAT_INTERVAL_ENV_VAR,
    run_sync_with_heartbeat,
)


def _slow_call(delay: float, value: str = "ok") -> str:
    time.sleep(delay)
    return value


@activity.defn(name="sandbox_runtime_heartbeat_probe")
async def _heartbeat_probe(delay: float) -> str:
    return await run_sync_with_heartbeat(
        _slow_call,
        delay,
        stage="coder",
        work_item_id="wi-heartbeat",
        operation="substrate_turn_1",
        interval_seconds=0.02,
    )


def test_long_sync_call_emits_periodic_real_temporal_heartbeats():
    env = ActivityEnvironment()
    heartbeats: list[dict] = []
    env.on_heartbeat = lambda details: heartbeats.append(details)

    result = asyncio.run(env.run(_heartbeat_probe, 0.09))

    assert result == "ok"
    states = [item["state"] for item in heartbeats]
    assert states[0] == "started"
    assert "running" in states, "a chamada bloqueante precisa permitir heartbeat durante o turno"
    assert states[-1] == "completed"
    assert [item["sequence"] for item in heartbeats] == list(range(len(heartbeats)))
    assert all(item["work_item_id"] == "wi-heartbeat" for item in heartbeats)
    assert all("instruction" not in item and "virtual_key" not in item for item in heartbeats)


def test_same_helper_works_outside_activity_without_calling_heartbeat(monkeypatch):
    def forbidden_heartbeat(*_args):
        raise AssertionError("heartbeat não pode ser chamado fora de Activity")

    monkeypatch.setattr(activity, "heartbeat", forbidden_heartbeat)
    assert asyncio.run(
        run_sync_with_heartbeat(
            _slow_call,
            0.01,
            "outside",
            stage="coder",
            work_item_id="wi-outside",
            operation="substrate_turn_1",
            interval_seconds=0.01,
        )
    ) == "outside"


def test_sync_exception_is_not_swallowed_outside_activity():
    def explode() -> None:
        raise LookupError("substrate failed")

    with pytest.raises(LookupError, match="substrate failed"):
        asyncio.run(
            run_sync_with_heartbeat(
                explode,
                stage="reviewer",
                work_item_id="wi-fail",
                operation="reviewer_verdict",
                interval_seconds=0.01,
            )
        )


@pytest.mark.parametrize("invalid", ["zero", "0", "-1"])
def test_invalid_heartbeat_interval_fails_before_starting_work(monkeypatch, invalid):
    called = False

    def work() -> None:
        nonlocal called
        called = True

    monkeypatch.setenv(HEARTBEAT_INTERVAL_ENV_VAR, invalid)
    with pytest.raises(ValueError, match="DSE_ACTIVITY_HEARTBEAT_INTERVAL_SECONDS"):
        asyncio.run(
            run_sync_with_heartbeat(
                work,
                stage="tester",
                work_item_id="wi-invalid",
                operation="run_tests",
            )
        )
    assert called is False

