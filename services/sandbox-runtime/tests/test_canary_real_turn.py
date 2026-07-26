"""NIGHTLY canary for a real turn (plano 09, Fase 4) — the structural answer to
the series of "found on a real run" commits: every bug of the "only shows up
with the real model" class now shows up here, at night, under a capped budget —
not on a customer run.

NEVER runs in the normal matrix (it costs money): requires DSE_CANARY=1 plus the
model-gateway up with a real provider (compose wsd + secrets). The
.github/workflows/canary-real-turn.yml workflow schedules this daily once the
repo has a remote + secrets configured.
"""
from __future__ import annotations

import asyncio
import os

import pytest
from dse_contracts import ProvisionSandboxInput, RunCoderTurnInput, TeardownSandboxInput

pytestmark = pytest.mark.skipif(
    os.environ.get("DSE_CANARY") != "1",
    reason="real-turn canary: only with DSE_CANARY=1 (real gateway + cost)",
)

CANARY_BUDGET_USD = float(os.environ.get("DSE_CANARY_BUDGET_USD", "0.50"))


def test_one_real_coder_turn_produces_a_diff_within_budget(work_item_id, state_dir, monkeypatch):
    monkeypatch.setenv("DSE_CODER_SUBSTRATE", os.environ.get("DSE_CODER_SUBSTRATE", "claude-agent"))
    from sandbox_runtime.activities import _paths_for, _run_coder_turn_impl, provision_sandbox, teardown_sandbox

    tenant_id = "tenant-canary"
    asyncio.run(provision_sandbox(ProvisionSandboxInput(work_item_id=work_item_id, tenant_id=tenant_id)))
    workspace_dir, _ = _paths_for(work_item_id)
    with open(os.path.join(workspace_dir, "app.py"), "w") as fh:
        fh.write("def add(a, b):\n    return a + b\n")

    try:
        result = asyncio.run(
            _run_coder_turn_impl(
                RunCoderTurnInput(
                    work_item_id=work_item_id,
                    tenant_id=tenant_id,
                    instruction=(
                        "In app.py, add a function `sub(a, b)` returning a - b. "
                        "Change nothing else."
                    ),
                )
            )
        )
        # the REAL turn produced a diff, cost money and stayed within the cap
        assert result.files_changed, "the real turn produced no diff at all"
        assert result.cost_usd > 0, "zero cost: the gateway was not really exercised"
        assert result.cost_usd <= CANARY_BUDGET_USD, (
            f"canary blew the budget: ${result.cost_usd:.4f} > ${CANARY_BUDGET_USD:.2f}"
        )
    finally:
        asyncio.run(teardown_sandbox(TeardownSandboxInput(work_item_id=work_item_id, tenant_id=tenant_id)))
