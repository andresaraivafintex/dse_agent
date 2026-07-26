"""S7 (Fase 5) — boundary test of the workflow -> checkpoint/rebuild pair.

The call sites of these Activities live in the workflow (WS-B,
`_checkpoint_or_rebuild`); the input models live here (WS-C). The WS-B test
fakes were lenient and hid divergences (the workflow called `checkpoint_sandbox`
without `tenant_id` and `rebuild_sandbox` without `tenant_id`/`checkpoint_ref`
-> `Failed decoding arguments` only in real runtime). These tests validate the
LITERAL PAYLOAD the workflow builds against the pydantic model, so that this
class of bug fails in CI, not in production.

If the workflow changes the payload shape, UPDATE the literals here on purpose.
"""
from __future__ import annotations

from sandbox_runtime.activities import (
    CheckpointSandboxInput,
    RebuildSandboxInput,
    TeardownSandboxInput,
)
from dse_contracts import CheckpointRef


def test_checkpoint_input_accepts_exact_workflow_payload():
    # literal from workflows.py::_checkpoint_or_rebuild (the checkpoint call site).
    payload = {
        "sandbox_id": "sbx-123",  # extra, ignored by the model
        "work_item_id": "wi-1",
        "tenant_id": "tnt-1",
        "phase": "implementing",
    }
    inp = CheckpointSandboxInput(**payload)
    assert inp.work_item_id == "wi-1"
    assert inp.tenant_id == "tnt-1"
    assert inp.phase == "implementing"


def test_rebuild_input_accepts_exact_workflow_payload():
    # the workflow passes the CheckpointRef of a successful checkpoint (a dict,
    # since execute_activity without result_type returns the raw deserialized
    # payload).
    checkpoint_ref = CheckpointRef(
        work_item_id="wi-1", git_ref="abc123", phase="implementing"
    ).model_dump()
    payload = {
        "work_item_id": "wi-1",
        "tenant_id": "tnt-1",
        "checkpoint_ref": checkpoint_ref,  # dict -> coerced into CheckpointRef
    }
    inp = RebuildSandboxInput(**payload)
    assert inp.checkpoint_ref.git_ref == "abc123"
    assert inp.tenant_id == "tnt-1"


def test_teardown_input_accepts_exact_workflow_payloads():
    # Post-S7 audit: the 4 teardown call sites were sending
    # {sandbox_id, work_item_id, reason} — tenant_id (required) was missing and
    # `reason` is not a field of the model (the real field is `stage`). No
    # teardown ran in production: orphan sandboxes. Literals of the fixed call
    # sites:
    for stage in ("cancelled_by_operator", "l1_retry_cap_exhausted", "done"):
        inp = TeardownSandboxInput(
            **{"work_item_id": "wi-1", "tenant_id": "tnt-1", "stage": stage}
        )
        assert inp.tenant_id == "tnt-1"
        assert inp.stage == stage
