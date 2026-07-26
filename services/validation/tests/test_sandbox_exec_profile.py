from __future__ import annotations

import pytest

from dse_contracts import SandboxHandle

from dse_validation.sandbox_exec import executor_for_handle


def test_production_refuses_inprocess_executor(monkeypatch):
    monkeypatch.setenv("DSE_DEPLOYMENT_PROFILE", "production")
    monkeypatch.setenv("DSE_SANDBOX_INPROCESS", "1")
    handle = SandboxHandle(
        sandbox_id="sb-1",
        work_item_id="wi-1",
        tenant_id="tenant-1",
        branch="dse/wi-1",
    )

    with pytest.raises(RuntimeError, match="refuses DSE_SANDBOX_INPROCESS"):
        executor_for_handle(handle)
