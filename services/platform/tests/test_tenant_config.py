"""tenant_config (migrations/0007_wsf.sql) — budgets/fairness/kill-switch por
tenant. Roda contra o Postgres real da fundação (nunca mockado)."""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from dse_audit import reconstruct_work_item_history  # noqa: F401 (paridade de import, não usado diretamente aqui)
from dse_audit.client import get_connection
from dse_platform import get_tenant_config, set_kill_switch, upsert_tenant_config


@pytest.fixture()
def tenant_id():
    return f"acme-wsf-{uuid.uuid4().hex[:8]}"


def test_get_tenant_config_returns_none_when_not_provisioned(tenant_id):
    assert get_tenant_config(tenant_id) is None


def test_upsert_creates_with_defaults_then_updates(tenant_id):
    created = upsert_tenant_config(tenant_id)
    assert created.tenant_id == tenant_id
    assert created.monthly_budget_usd == Decimal("100.00")
    assert created.max_concurrent_work_items == 5
    assert created.kill_switch_enabled is False

    updated = upsert_tenant_config(tenant_id, monthly_budget_usd=Decimal("250.00"))
    assert updated.monthly_budget_usd == Decimal("250.00")
    # campo não passado permanece o que já estava (idempotência parcial)
    assert updated.max_concurrent_work_items == 5


def test_upsert_is_idempotent(tenant_id):
    first = upsert_tenant_config(tenant_id, monthly_budget_usd=Decimal("42.00"), max_concurrent_work_items=3)
    second = upsert_tenant_config(tenant_id, monthly_budget_usd=Decimal("42.00"), max_concurrent_work_items=3)
    assert first == second


def test_set_kill_switch_requires_reason_when_enabling(tenant_id):
    with pytest.raises(ValueError):
        set_kill_switch(tenant_id, enabled=True, reason=None, actor="system:test")


def test_set_kill_switch_enable_then_disable_and_audit_trail(tenant_id):
    upsert_tenant_config(tenant_id)

    enabled_cfg = set_kill_switch(
        tenant_id, enabled=True, reason="budget exceeded", actor="system:budget-monitor"
    )
    assert enabled_cfg.kill_switch_enabled is True
    assert enabled_cfg.kill_switch_reason == "budget exceeded"

    disabled_cfg = set_kill_switch(tenant_id, enabled=False, reason=None, actor="system:operator")
    assert disabled_cfg.kill_switch_enabled is False

    # P8: cada mudança de kill-switch deixou uma linha de audit imutável.
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT action, actor, details FROM audit_log WHERE tenant_id = %s ORDER BY ts ASC, id ASC",
                (tenant_id,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    assert len(rows) == 2
    assert rows[0][0] == "kill_switch_enabled"
    assert rows[0][1] == "system:budget-monitor"
    assert rows[0][2] == {"reason": "budget exceeded"}
    assert rows[1][0] == "kill_switch_disabled"
    assert rows[1][1] == "system:operator"
