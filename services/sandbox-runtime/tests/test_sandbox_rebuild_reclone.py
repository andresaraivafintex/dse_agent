"""A rebuild must be able to re-clone, because the checkpoint usually did not survive.

The checkpoint volume is an emptyDir unless a PVC is configured, and the chart
ships `checkpointPvc.enabled: false` — so when a rebuild creates a fresh Pod,
both /workspace and /checkpoint.git come up empty. `_checkpoint_has_branch` is
then False, and with no `repo` in the provision request the in-Pod bootstrap fell
through to its last branch and initialised an EMPTY git repo. The Coder ran
against a workspace holding none of the customer's code, and every retry
reproduced that state exactly: fourteen identical failures on the VPS.

What is pinned here is the one thing that changed — `rebuild_sandbox` forwards
`repo`/`base_branch`, so the bootstrap has something to fall back to.
"""
from __future__ import annotations

import asyncio

import sandbox_runtime.activities as acts
# Imported from the ACTIVITY module, not from dse_contracts: that is the type
# Temporal decodes the payload into, and an earlier version of this test used
# the contract type — so it passed while production raised AttributeError on a
# field the activity's own model did not have.
from sandbox_runtime.activities import RebuildSandboxInput


class _Driver:
    """Records what the rebuild was asked to provision."""

    workspace_is_host_visible = False

    def __init__(self):
        self.request = None

    def rebuild(self, req):
        self.request = req.provision
        raise RuntimeError("stop here — the provision request is what this test reads")


def test_the_rebuild_forwards_the_repo_so_the_bootstrap_can_reclone(monkeypatch):
    driver = _Driver()
    monkeypatch.setattr(acts, "select_sandbox_driver", lambda *a, **k: driver)
    monkeypatch.setattr(acts, "validate_runtime_startup", lambda *a, **k: None)

    inp = RebuildSandboxInput(
        work_item_id="wi-1", tenant_id="t",
        checkpoint_ref={"work_item_id": "wi-1", "git_ref": "refs/heads/dse/task-1", "phase": "base"},
        branch="dse/task-1", repo="acme/api", base_branch="main",
    )
    try:
        asyncio.run(acts.rebuild_sandbox(inp))
    except Exception:  # noqa: BLE001 — the driver stops the flow deliberately
        pass

    assert driver.request is not None, "the rebuild never reached the driver"
    assert driver.request.repo == "acme/api", (
        "the rebuild did not forward the repo — a checkpoint-less rebuild would "
        "initialise an EMPTY workspace instead of re-cloning"
    )
    assert driver.request.base_branch == "main"
