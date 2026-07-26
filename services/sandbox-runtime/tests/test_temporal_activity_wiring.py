"""WSC-E1-T3: proves that `provision_sandbox`/`checkpoint_sandbox`/
`rebuild_sandbox`/`teardown_sandbox`/`run_coder_turn` are real Temporal Python
SDK Activities — not just async functions that happen to share the same
signature.

Uses `temporalio.testing.ActivityEnvironment`, the SDK's OFFICIAL harness for
running an Activity in isolation (outside a full Workflow/Worker) — it is not a
mock: it is the real SDK's own Activity execution code, just without needing a
Temporal server up. Environment note: the `dse_temporal` container of this
parallel development session is missing the dynamic config schema in the image
(`temporalio/auto-setup:1.24`) and exited (`docker ps -a` shows `Exited (1)`);
that is a foundation infra problem (docker-compose.yml, outside WS-C's editing
scope) — it does not affect the validity of this test, which does not depend on
the Temporal server, only on the SDK.

The idempotency/chaos/scoped-git tests already call these same functions
directly via `asyncio.run(...)` against real Docker/Postgres/git — this file
specifically covers the integration with the Temporal SDK itself (registration
via `@activity.defn(name=...)`, execution via `ActivityEnvironment`).
"""
from __future__ import annotations

import asyncio

from temporalio.testing import ActivityEnvironment

from dse_contracts import (
    ACTIVITY_CHECKPOINT_SANDBOX,
    ACTIVITY_PROVISION_SANDBOX,
    ACTIVITY_REBUILD_SANDBOX,
    ACTIVITY_RUN_CODER_TURN,
    ACTIVITY_TEARDOWN_SANDBOX,
)
from sandbox_runtime.activities import (
    ProvisionSandboxInput,
    TeardownSandboxInput,
    checkpoint_sandbox,
    provision_sandbox,
    rebuild_sandbox,
    run_coder_turn,
    teardown_sandbox,
)


def test_activity_names_match_the_contract():
    assert provision_sandbox.__temporal_activity_definition.name == ACTIVITY_PROVISION_SANDBOX
    assert checkpoint_sandbox.__temporal_activity_definition.name == ACTIVITY_CHECKPOINT_SANDBOX
    assert rebuild_sandbox.__temporal_activity_definition.name == ACTIVITY_REBUILD_SANDBOX
    assert teardown_sandbox.__temporal_activity_definition.name == ACTIVITY_TEARDOWN_SANDBOX
    assert run_coder_turn.__temporal_activity_definition.name == ACTIVITY_RUN_CODER_TURN


def test_provision_and_teardown_run_through_real_temporal_activity_environment(work_item_id, state_dir):
    """Runs the Activity through the real Temporal SDK harness
    (`ActivityEnvironment.run`), not just as a loose Python function."""
    env = ActivityEnvironment()

    handle = asyncio.run(
        env.run(provision_sandbox, ProvisionSandboxInput(work_item_id=work_item_id, tenant_id="tenant-a"))
    )
    assert handle.container_id
    assert handle.work_item_id == work_item_id

    asyncio.run(env.run(teardown_sandbox, TeardownSandboxInput(work_item_id=work_item_id, tenant_id="tenant-a")))
