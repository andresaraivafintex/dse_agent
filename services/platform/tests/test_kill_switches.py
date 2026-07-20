"""WSF-E6-T2 — kill switches nos 4 escopos + quarentena. Postgres real."""
from __future__ import annotations

import uuid

import pytest
from dse_audit.client import get_connection
from dse_platform import (
    is_admission_blocked,
    is_quarantined,
    quarantine_work_item,
    release_quarantine,
    set_channel_kill_switch,
    set_global_kill_switch,
    set_tenant_kill_switch,
    upsert_tenant_config,
)
from dse_platform.kill_switches import get_global_kill_switch


@pytest.fixture()
def tenant():
    return f"acme-ks-{uuid.uuid4().hex[:8]}"


def _actions(tenant_id):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT action FROM audit_log WHERE tenant_id = %s ORDER BY ts, id", (tenant_id,))
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def test_tenant_scope_blocks_admission(tenant):
    upsert_tenant_config(tenant)
    assert is_admission_blocked(tenant) is None
    set_tenant_kill_switch(tenant, enabled=True, reason="over budget", actor="system:test")
    block = is_admission_blocked(tenant)
    assert block is not None and block.scope == "tenant"
    assert block.reason == "over budget"


def test_channel_scope_blocks_only_that_channel(tenant):
    upsert_tenant_config(tenant)
    set_channel_kill_switch(tenant, "C-bad", active=True, reason="spam", actor="system:test")
    assert is_admission_blocked(tenant, "C-bad").scope == "channel"
    assert is_admission_blocked(tenant, "C-ok") is None
    assert "channel_kill_switch_enabled" in _actions(tenant)


def test_global_scope_blocks_all_tenants(tenant):
    upsert_tenant_config(tenant)
    try:
        set_global_kill_switch(enabled=True, reason="incident", actor="system:test")
        block = is_admission_blocked(tenant)
        assert block is not None and block.scope == "global"
    finally:
        # não deixar o global ligado (afetaria outros testes/tenants)
        set_global_kill_switch(enabled=False, reason=None, actor="system:test")
    assert get_global_kill_switch()[0] is False


def test_global_precedence_over_tenant(tenant):
    upsert_tenant_config(tenant)
    set_tenant_kill_switch(tenant, enabled=True, reason="t", actor="system:test")
    try:
        set_global_kill_switch(enabled=True, reason="g", actor="system:test")
        # global é mais amplo -> aparece primeiro
        assert is_admission_blocked(tenant).scope == "global"
    finally:
        set_global_kill_switch(enabled=False, reason=None, actor="system:test")


def test_enabling_requires_reason(tenant):
    with pytest.raises(ValueError):
        set_global_kill_switch(enabled=True, reason=None, actor="system:test")
    with pytest.raises(ValueError):
        set_channel_kill_switch(tenant, "C", active=True, reason=None, actor="system:test")


def test_quarantine_lifecycle(tenant):
    wid = f"wi-{uuid.uuid4().hex[:8]}"
    assert is_quarantined(wid) is False
    quarantine_work_item(wid, tenant, reason="suspicious diff", actor="usr_operator1")
    assert is_quarantined(wid) is True
    release_quarantine(wid, tenant, actor="usr_operator1")
    assert is_quarantined(wid) is False
    acts = _actions(tenant)
    assert "work_item_quarantined" in acts
    assert "work_item_quarantine_released" in acts


def test_quarantine_requires_reason(tenant):
    with pytest.raises(ValueError):
        quarantine_work_item(f"wi-{uuid.uuid4().hex[:6]}", tenant, reason="", actor="usr_x")
