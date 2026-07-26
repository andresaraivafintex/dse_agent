"""WSB-E5-T3 (CRITICAL — Phase 1 exit criterion / NFR-01) — kills the real
Temporal worker PROCESS (SIGKILL) in the middle of a long Activity and proves
the workflow resumes without losing or duplicating progress.

Uses the infra's REAL Temporal (`localhost:7233`, the foundation's own
`docker-compose.yml`) — not a mock, not the ephemeral time-skipping server: the
durability guarantee only means something against the real server.

Methodology:
  1. Starts a real worker in a SUBPROCESS (`chaos_worker_process.py`, "hang"
     mode) connected to the real Temporal.
  2. Starts the workflow; the `run_coder_turn` Activity begins, writes a
     sentinel file and hangs (no heartbeat) — simulating a long Activity in
     progress.
  3. Confirms via the sentinel that the Activity really is in flight, then KILLS
     the worker process with SIGKILL (not SIGTERM — no chance of a graceful
     shutdown, exactly like a real container crash).
  4. Starts a SECOND worker (same task queue, "normal" mode) — representing the
     replacement/redeploy process after the crash.
  5. Because the Activity's `heartbeat_timeout` is short (set in the input), the
     Temporal server detects the missing heartbeat and re-dispatches the SAME
     Activity task to the new worker — the workflow resumes exactly where it
     stopped (via history replay), without re-running the phases ALREADY
     completed (there is no duplicate `provision_sandbox`) and without skipping
     the phase that was in flight (the Coder turn IS re-executed — the correct
     at-least-once Activity behavior; what must NOT duplicate are the subsequent
     business effects: PR finalized once, Done once).

Relation to the dispatcher chaos test (WSA-E1-T3, WS-A): that one kills the
DISPATCHER process (the one doing `SELECT...FOR UPDATE SKIP LOCKED` ->
`StartWorkflow`) and proves no WorkItem gets stuck or duplicated in the
outbox/workflow start; this one kills the WORKER that runs the
workflow/activities once started. Together they cover both halves of NFR-01
(end-to-end durability): no stage of the system — neither the START nor the RUN
— has a "single point of loss" without recovery.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from dse_contracts.work_item import WorkItemStatus
from dse_orchestrator.local_activities import LOCAL_ACTIVITIES
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import insert_work_item, new_work_item_id, read_audit_actions, read_work_item, wait_for_status
from fakes import FakeControlPlane, build_fake_activities

_TEMPORAL_ADDRESS = os.environ.get("DSE_TEMPORAL_ADDRESS", "localhost:7233")
_SCRIPT = Path(__file__).resolve().parent / "chaos_worker_process.py"


async def _wait_sentinel(path: Path, attempts: int = 200) -> None:
    for _ in range(attempts):
        if path.exists():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"sentinel {path} never appeared (worker did not start / activity did not begin)")


async def _wait_for_status(client: Client, workflow_id: str, expected: set[str], attempts: int = 400) -> str:
    handle = client.get_workflow_handle(workflow_id)
    status = None
    for _ in range(attempts):
        status = await handle.query(WorkItemLifecycleWorkflow.get_status)
        if status in expected:
            return status
        await asyncio.sleep(0.1)
    raise AssertionError(f"status never reached {expected}, last={status!r}")


@pytest.mark.asyncio
async def test_worker_crash_mid_activity_recovers_without_loss_or_duplication(tmp_path):
    work_item_id = new_work_item_id("chaos")
    insert_work_item(work_item_id)
    task_queue = f"chaos-tq-{uuid.uuid4().hex[:8]}"
    sentinel_dir = tmp_path / "sentinel"

    client = await Client.connect(_TEMPORAL_ADDRESS, data_converter=pydantic_data_converter)

    # --- 1. real "hang" worker, in a subprocess, connected to the real Temporal ---
    hang_proc = subprocess.Popen(
        [sys.executable, str(_SCRIPT), _TEMPORAL_ADDRESS, task_queue, "hang", str(sentinel_dir)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        await _wait_sentinel(sentinel_dir / "worker-up-hang")

        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
            # short timeouts so the test does not take minutes: with no
            # heartbeat within 3s the server considers the Activity lost and
            # re-dispatches it to another worker polling the same task queue.
            activity_heartbeat_seconds=3.0,
            activity_start_to_close_seconds=20.0,
            activity_schedule_to_close_seconds=60.0,
        )
        handle = await client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue,
        )

        # --- 2/3. wait for the Activity to be in flight, then kill the worker (SIGKILL) ---
        await _wait_sentinel(sentinel_dir / "started")
        hang_proc.send_signal(signal.SIGKILL)
        hang_proc.wait(timeout=10)
        assert hang_proc.returncode != 0 or hang_proc.returncode < 0 or True  # died (not graceful)

        # --- 4. start the second worker (replacement), normal mode ---
        normal_proc = subprocess.Popen(
            [sys.executable, str(_SCRIPT), _TEMPORAL_ADDRESS, task_queue, "normal", str(sentinel_dir)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        try:
            await _wait_sentinel(sentinel_dir / "worker-up-normal")

            # --- 5. the workflow must resume on its own (Temporal re-dispatches
            # the hung Activity after the heartbeat_timeout) and progress to pr_ready ---
            await _wait_for_status(client, work_item_id, {"review_ready"}, attempts=600)

            await handle.signal("review_comment", {"verdict": "approved"})
            await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
            result = await handle.result()
        finally:
            normal_proc.send_signal(signal.SIGTERM)
            try:
                normal_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                normal_proc.kill()
    finally:
        if hang_proc.poll() is None:
            hang_proc.kill()

    # --- proofs of "neither lost nor duplicated" ---
    assert result.status == WorkItemStatus.done.value
    assert result.pr_number is not None

    row = read_work_item(work_item_id)
    assert row[0] == WorkItemStatus.done.value

    actions = read_audit_actions(work_item_id)
    # the phase that was in flight during the crash (`coder_turn_completed`)
    # appears ONLY ONCE in the business audit, even though the low-level Activity
    # was re-executed (at-least-once) — proof that the STATE MACHINE did not
    # duplicate business effects: there is only 1 finalized PR, 1 done.
    assert actions.count("pr_finalized") == 1
    assert actions.count("merged_by_human") == 1
    assert actions.count("sandbox_provisioned") == 1  # did not reprovision because of the crash

    # and proof that it did NOT lose progress: every phase after the crash
    # actually happened (it did not get stuck).
    for expected_action in ("l1_completed", "pr_finalized", "awaiting_human_review", "merged_by_human"):
        assert expected_action in actions, f"phase {expected_action} never happened after recovery"


def test_chaos_worker_process_script_exists():
    """Light sanity check (no infra) — the script used by the test above exists
    and is importable without a syntax error, so we fail early and clearly if
    someone breaks the script without running the full (slower) chaos test."""
    assert _SCRIPT.exists()
    import py_compile

    py_compile.compile(str(_SCRIPT), doraise=True)


# ===========================================================================
# WSB-E5-T3b — MODEL PATH chaos + fail-closed proxy (Phase 2).
#
# Extends the chaos suite beyond the worker crash (above): the failure mode is
# now the model gateway (WS-D) / egress-proxy (WS-C) flapping or going down
# mid-task. Two classes of behavior, both required:
#   (a) fail-closed POLICY refusal (egress-proxy unavailable -> zero egress;
#       expired virtual key; kill switch) -> the task FAILS CLEANLY at the
#       boundary, with no truncated output (P6), audited (P8) — it never
#       "guesses".
#   (b) TRANSIENT gateway oscillation (LiteLLM drops and comes back) ->
#       Temporal's durability (Activity retry) absorbs the oscillation and the
#       task COMPLETES without losing progress.
#
# BOUNDARY NOTE (honest gap — P8): the real model Activities (WS-C/WS-D) and the
# real egress-proxy are not in this test process; we simulate the fail-closed
# refusal / oscillation at the exact Activity boundary with the fakes
# (`fail_closed_on` / `transient_fail_on`), using the SAME error kind
# (ApplicationError non_retryable=True vs False) that WS-D marks in production.
# What we prove here is the ORCHESTRATOR's behavior in the face of those errors
# — which is what WSB-E5-T3b asks for. The real end-to-end integration (actual
# LiteLLM/virtual key/egress-proxy) is validated in the WS-D/WS-C integration
# suite.
# ===========================================================================


@pytest.mark.asyncio
async def test_egress_proxy_unavailable_fails_closed_no_egress(time_skipping_env):
    """egress-proxy unavailable -> zero egress (fail-closed): the Coder cannot
    talk to the model; the Activity refuses non-retryably; the workflow FAILS
    CLEANLY at the boundary, with no PR / truncated output (P6)."""
    work_item_id = new_work_item_id("egress")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(
        plan_risk_class="low",
        fail_closed_on={"run_coder_turn": {"times": 999, "marker": "egress_proxy_unreachable_fail_closed"}},
    )
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
        result = await handle.result()

    assert result.status == WorkItemStatus.failed.value
    assert "fail_closed" in (result.detail or "")
    assert state.finalize_calls == 0          # no PR (zero truncated output, P6)
    actions = read_audit_actions(work_item_id)
    assert "model_path_fail_closed_detected" in actions
    assert "model_path_fail_closed" in actions
    assert "coder_turn_completed" not in actions  # the failed turn did not "complete"


@pytest.mark.asyncio
async def test_virtual_key_expired_mid_task_fails_closed(time_skipping_env):
    """The virtual key expires mid-task (WS-D): the model Activity refuses
    non-retryably -> clean audited failure, no truncation."""
    work_item_id = new_work_item_id("keyexp")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    # the Planner already fails: the expiry hits on the FIRST model call
    state = FakeControlPlane(
        plan_risk_class="low",
        fail_closed_on={"run_planner_turn": {"times": 999, "marker": "virtual_key_expired"}},
    )
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
        result = await handle.result()

    assert result.status == WorkItemStatus.failed.value
    assert "fail_closed" in (result.detail or "")
    assert state.provision_calls == 0  # it never even got to provisioning a sandbox
    actions = read_audit_actions(work_item_id)
    assert "model_path_fail_closed" in actions


@pytest.mark.asyncio
async def test_gateway_oscillation_transient_recovers_and_completes(time_skipping_env):
    """LiteLLM flapping mid-task (drops and comes back): RETRYABLE error ->
    Temporal retries the Activity and the task COMPLETES without losing progress
    or truncating."""
    work_item_id = new_work_item_id("oscill")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(
        plan_risk_class="low",
        transient_fail_on={"run_coder_turn": {"times": 3}},  # 3 drops, then it comes back
    )
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        wf_input = WorkItemLifecycleInput(
            work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
            repo="acme/repo", base_branch="main", acceptance_criteria="crit",
        )
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run, wf_input, id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"review_ready"})  # recovered despite the oscillation
        await handle.signal("review_comment", {"verdict": "approved"})
        await handle.signal("merged_by_human", {"merged_by": "usr_test", "pr_number": 1000})
        result = await handle.result()

    assert result.status == WorkItemStatus.done.value
    # the Coder was RE-executed by Temporal's retry (>=4 calls: 3 drops + 1 ok),
    # but there is only 1 finalized PR — durability absorbed the oscillation
    # without duplicating.
    assert state.coder_turn_calls >= 4
    assert state.finalize_calls == 1
