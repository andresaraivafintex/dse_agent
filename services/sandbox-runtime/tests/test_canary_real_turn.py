"""Canário NOTURNO de turno real (plano 09, Fase 4) — a resposta estrutural à
série de commits "achado do disparo real": todo bug da classe "só aparece com
o modelo de verdade" passa a aparecer aqui, à noite, com budget capado — não
no disparo com cliente.

NUNCA roda na matriz normal (custa dinheiro): exige DSE_CANARY=1 + o
model-gateway de pé com provider real (compose wsd + secrets). O workflow
.github/workflows/canary-real-turn.yml agenda isto diariamente quando o repo
tiver remoto + secrets configurados.
"""
from __future__ import annotations

import asyncio
import os

import pytest
from dse_contracts import ProvisionSandboxInput, RunCoderTurnInput, TeardownSandboxInput

pytestmark = pytest.mark.skipif(
    os.environ.get("DSE_CANARY") != "1",
    reason="canário de turno real: só com DSE_CANARY=1 (gateway real + custo)",
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
        # o turno REAL produziu diff, custou dinheiro e ficou dentro do cap
        assert result.files_changed, "turno real não produziu diff nenhum"
        assert result.cost_usd > 0, "custo zero: o gateway não foi exercitado de verdade"
        assert result.cost_usd <= CANARY_BUDGET_USD, (
            f"canário estourou o budget: ${result.cost_usd:.4f} > ${CANARY_BUDGET_USD:.2f}"
        )
    finally:
        asyncio.run(teardown_sandbox(TeardownSandboxInput(work_item_id=work_item_id, tenant_id=tenant_id)))
