"""Remediation — payload evolution invariant + replay determinism (canonical
spec §5).

Two guarantees that sprint 1 lacked (finding from the post-S7 audit):

1. **Input self-healing from the system of record.** Whoever starts the workflow
   may pass only the `work_item_id` (string) — the `_coerce_input` path resolves
   the state from Postgres via `load_work_item`. This is the same mechanism that
   heals an in-flight workflow whose historical payload lacks a new field: the
   value comes from the database, it is not invented by a default/fixture.

2. **Replay determinism.** The event history of a real execution is re-executed
   with `Replayer` against the CURRENT workflow definition. Any future change
   that breaks determinism (reordering commands, removing a `patched()`,
   changing payload shape without compat) fails HERE — which is exactly the test
   spec §5 requires ("a replay test using a history written by the previous
   shape"). The captured history is saved as a versioned fixture to serve as the
   "previous shape" on the next change.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Replayer, Worker

from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator.local_activities import LOCAL_ACTIVITIES
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import insert_work_item, new_work_item_id, read_audit_actions
from fakes import FakeControlPlane, build_fake_activities

_FIXTURE_DIR = Path(__file__).parent / "histories"


@pytest.mark.asyncio
async def test_string_start_input_self_heals_from_db(time_skipping_env):
    """Start with the `work_item_id` string (not the dataclass): the workflow
    resolves the state from Postgres instead of failing on an incomplete input —
    the same path that heals historical payloads missing the new field."""
    work_item_id = new_work_item_id("selfheal")
    insert_work_item(work_item_id)
    state = FakeControlPlane()
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"

    async with Worker(
        time_skipping_env.client,
        task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow],
        activities=list(LOCAL_ACTIVITIES) + build_fake_activities(state),
    ):
        # the input is the STRING, not WorkItemLifecycleInput — exercises _coerce_input.
        result = await time_skipping_env.client.execute_workflow(
            WorkItemLifecycleWorkflow.run,
            work_item_id,
            id=work_item_id,
            task_queue=task_queue,
        )

    actions = read_audit_actions(work_item_id)
    # Proof that the string branch resolved from the database and the workflow
    # REALLY ran (it could not audit intake without loading the WorkItem from
    # Postgres).
    assert "intake_started" in actions
    # With no acceptance criteria/content, the completeness gate (S2) parks on
    # clarification — a REAL lifecycle state, not an input error.
    assert result.status in {
        WorkItemStatus.needs_clarification.value,
        WorkItemStatus.escalated.value,
    }


@pytest.mark.asyncio
async def test_workflow_history_replays_deterministically(time_skipping_env):
    """Runs a real workflow, captures its event history and re-executes it with
    the Replayer against the current definition. Fails on any non-determinism.
    The history is saved as a versioned fixture (current shape) so the next
    change can replay it as the 'previous shape' (spec §5)."""
    work_item_id = new_work_item_id("replay")
    insert_work_item(work_item_id)
    # Short, deterministic path: a plan with no expected_files -> escalated, few
    # activities, small and stable history.
    state = FakeControlPlane(plan_expected_files=[])
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"

    async with Worker(
        time_skipping_env.client,
        task_queue=task_queue,
        workflows=[WorkItemLifecycleWorkflow],
        activities=list(LOCAL_ACTIVITIES) + build_fake_activities(state),
    ):
        result = await time_skipping_env.client.execute_workflow(
            WorkItemLifecycleWorkflow.run,
            _replay_input(work_item_id),
            id=work_item_id,
            task_queue=task_queue,
        )
        assert result.status == WorkItemStatus.escalated.value
        handle = time_skipping_env.client.get_workflow_handle(work_item_id)
        history = await handle.fetch_history()

    # 1) Replay the freshly captured history against the CURRENT definition.
    replayer = Replayer(
        workflows=[WorkItemLifecycleWorkflow],
        data_converter=pydantic_data_converter,
    )
    await replayer.replay_workflow(
        WorkflowHistory.from_json(work_item_id, _history_to_json(history))
    )

    # 2) Persist the versioned fixture (current shape) — the next shape change
    # replays THIS history as the "previous shape" required by spec §5.
    _FIXTURE_DIR.mkdir(exist_ok=True)
    fixture = _FIXTURE_DIR / "escalated_empty_plan.json"
    if not fixture.exists():
        fixture.write_text(json.dumps(_history_to_json(history), indent=2, sort_keys=True))


@pytest.mark.asyncio
async def test_committed_history_fixture_still_replays(time_skipping_env):
    """If a versioned history fixture of a PREVIOUS shape exists, it must stay
    replayable against the current definition — the real regression lock from
    spec §5. Clean skip while the fixture has not been captured yet."""
    fixture = _FIXTURE_DIR / "escalated_empty_plan.json"
    if not fixture.exists():
        pytest.skip("history fixture not captured yet (run the replay test first)")
    events = json.loads(fixture.read_text())
    replayer = Replayer(
        workflows=[WorkItemLifecycleWorkflow],
        data_converter=pydantic_data_converter,
    )
    await replayer.replay_workflow(WorkflowHistory.from_json("replay-fixture", events))


def _replay_input(work_item_id: str):
    from dse_orchestrator.models import WorkItemLifecycleInput

    return WorkItemLifecycleInput(
        work_item_id=work_item_id,
        tenant_id="test-tenant",
        requester="usr_test",
        repo="acme/repo",
        base_branch="main",
        acceptance_criteria="criterio verificavel",
        ci_poll_interval_seconds=0.01,
        ci_pending_poll_cap=10,
        activity_retry_cap=2,
    )


def _history_to_json(history) -> dict:
    """`WorkflowHistory.to_json()` does not exist on every version; serialize via
    each event's proto JSON, in the format `WorkflowHistory.from_json` accepts
    ({"events": [...]})."""
    from google.protobuf.json_format import MessageToDict

    return {"events": [MessageToDict(e) for e in history.events]}
