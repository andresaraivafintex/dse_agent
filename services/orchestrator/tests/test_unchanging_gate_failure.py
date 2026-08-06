"""The same gate complaint, round after round, has to stop the loop.

The no-op guard catches a Coder that writes nothing. It cannot catch the other
loop, which is the expensive one: the Coder DOES edit files, `head_sha` moves
every round, and the identical complaint comes back.

Measured on the Angular testbed — three consecutive rounds answered

    NG2: Type 'string' is not assignable to '"success" | "secondary" | ...'

on the same template binding, each after a real edit, each costing a full
Coder + Tester + L1 round (~12 minutes and a paid model turn) to be told the
same thing again.

`plan §8.5` calls this `repair_key` and writes it as
`hash(gate, command, normalized_error, head_sha)`. The head_sha is deliberately
left out of the implementation: with it the key can only repeat when the tree
did not move, and that case is already covered one branch earlier. Including it
would make this guard fire exactly where it is not needed and never where it is.
"""
from __future__ import annotations

import uuid

import pytest
from temporalio.worker import Worker

from dse_contracts import GateStatus, L1Finding
from dse_orchestrator.local_activities import LOCAL_ACTIVITIES
from dse_orchestrator.models import WorkItemLifecycleInput
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

from conftest import insert_work_item, new_work_item_id, read_audit_actions, wait_for_status
from fakes import FakeControlPlane, build_fake_activities


def _wf_input(work_item_id: str, **kw) -> WorkItemLifecycleInput:
    return WorkItemLifecycleInput(
        work_item_id=work_item_id, tenant_id="test-tenant", requester="usr_test",
        repo="acme/repo", base_branch="main", acceptance_criteria="crit", **kw,
    )


def _finding(detail: str) -> L1Finding:
    return L1Finding(check="build", passed=False, status=GateStatus.FAIL,
                     detail=detail, summary="build failed (exit=1)")


# ---------------------------------------------------------------------------
# The fingerprint itself — a pure function, so pin it directly.
# ---------------------------------------------------------------------------
def test_the_same_complaint_hashes_the_same_after_the_file_moves():
    """A complaint that merely shifted by a line, or took 49.6s instead of
    47.2s, is the SAME complaint. Digits are normalised away for exactly this:
    without it every round looks like progress."""
    fp = WorkItemLifecycleWorkflow._repair_fingerprint
    a = _finding("badge.component.html:3:3 NG2: Type 'string' is not assignable [47.2s]")
    b = _finding("badge.component.html:9:5 NG2: Type 'string' is not assignable [49.6s]")
    assert fp([a]) == fp([b])


def test_a_different_complaint_hashes_differently():
    """The counter must reset on progress: fixing one error and hitting another
    is not a stuck Coder, and escalating there would abandon a run that was
    still converging."""
    fp = WorkItemLifecycleWorkflow._repair_fingerprint
    assert fp([_finding("NG2: Type 'string' is not assignable")]) != fp(
        [_finding("NG5: Property 'severity' does not exist")]
    )


def test_the_fingerprint_does_not_collapse_every_build_failure():
    """`summary` is `build failed (exit=1)` for every build failure there has
    ever been. Hashing that would escalate a Coder that fixed one error and hit
    a different one, so the fingerprint has to read `detail`."""
    fp = WorkItemLifecycleWorkflow._repair_fingerprint
    a = L1Finding(check="build", passed=False, status=GateStatus.FAIL,
                  detail="NG2: severity", summary="build failed (exit=1)")
    b = L1Finding(check="build", passed=False, status=GateStatus.FAIL,
                  detail="NG5: missing property", summary="build failed (exit=1)")
    assert fp([a]) != fp([b])


# ---------------------------------------------------------------------------
# The loop it breaks, driven through the real workflow.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_three_identical_l1_failures_escalate_instead_of_looping(time_skipping_env):
    """Two repairs, then stop — `plan §8.5` sets the budget as two repairs in
    TOTAL. The Coder edits files every round, so the no-op guard never fires;
    only the fingerprint can end this."""
    work_item_id = new_work_item_id("stuck")
    insert_work_item(work_item_id)
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    state = FakeControlPlane(
        plan_risk_class="low",
        l1_fail_times=9,                       # it would never pass on its own
        coder_files_changed=["app.py"],        # the tree moves every round
    )
    activities = list(LOCAL_ACTIVITIES) + build_fake_activities(state)

    async with Worker(time_skipping_env.client, task_queue=task_queue,
                      workflows=[WorkItemLifecycleWorkflow], activities=activities):
        handle = await time_skipping_env.client.start_workflow(
            WorkItemLifecycleWorkflow.run,
            _wf_input(work_item_id, coder_retry_cap=99),
            id=work_item_id, task_queue=task_queue)
        await wait_for_status(handle, {"escalated", "failed"})

        assert "escalated" in read_audit_actions(work_item_id)
        # 3 L1 runs = the initial failure plus two repairs. A fourth means the
        # guard did not fire; fewer means it fired too early.
        assert state.l1_calls == 3, (
            f"L1 ran {state.l1_calls} times for the same complaint"
        )
