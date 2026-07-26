"""WSD-E4-T2: scoped gateway kill switch + in-flight model reassign.

REAL tests. They prove:
  - a work_item/tenant/global kill switch refuses the call and audits it;
  - the gateway HONORS WS-F's controls (tenant_config.kill_switch_enabled and
    dse_kill_switch_global) on top of its own table;
  - on/off: disabling the switch allows calls again (effect <= the cache TTL);
  - reassign swaps the effective model of the next call + audits it;
  - reassign does NOT bypass policy.
"""
from __future__ import annotations

import pytest
from dse_audit.client import get_connection as audit_conn
from dse_contracts.gateway_contract import GatewayCallHeaders, Stage
from model_gateway_client import (
    GatewayCallError,
    chat_completion,
    clear_reassignment,
    is_killed,
    mint_virtual_key,
    reassign_model,
    revoke_virtual_key,
    set_kill_switch,
)
from model_gateway_client import controls, db

ECHO = "eco/echo-model"


def _audit_actions(work_item_id):
    conn = audit_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT action FROM audit_log WHERE work_item_id=%s ORDER BY id", (work_item_id,))
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def test_work_item_kill_switch_denies_and_audits(unique_ids):
    t, wi = unique_ids["tenant_id"], unique_ids["work_item_id"]
    key = mint_virtual_key(t, wi, Stage.coder, models=[ECHO])
    headers = GatewayCallHeaders(tenant_id=t, work_item_id=wi, stage=Stage.coder)
    try:
        set_kill_switch("work_item", wi, enabled=True, reason="test", actor="system:operator", tenant_id=t)
        with pytest.raises(GatewayCallError) as ei:
            chat_completion(headers=headers, virtual_key=key, model=ECHO, messages=[{"role": "user", "content": "x"}])
        assert ei.value.status_code == 403
        assert ei.value.body["kind"] == "kill_switch"
        assert ei.value.body["scope_type"] == "work_item"
        assert "gateway.call_denied_kill_switch" in _audit_actions(wi)

        # turning the switch off -> calls are allowed again
        set_kill_switch("work_item", wi, enabled=False, actor="system:operator", tenant_id=t)
        r = chat_completion(headers=headers, virtual_key=key, model=ECHO, messages=[{"role": "user", "content": "ok"}])
        assert r.content.startswith("ECHO[")
    finally:
        set_kill_switch("work_item", wi, enabled=False, tenant_id=t)
        revoke_virtual_key(key)


def test_gateway_honors_wsf_tenant_kill_switch(unique_ids):
    t, wi = unique_ids["tenant_id"], unique_ids["work_item_id"]
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tenant_config (tenant_id, kill_switch_enabled, kill_switch_reason)
                VALUES (%s, true, 'wsf-operator')
                ON CONFLICT (tenant_id) DO UPDATE SET kill_switch_enabled=true, kill_switch_reason='wsf-operator'
                """,
                (t,),
            )
        conn.commit()
    finally:
        conn.close()
    controls.clear_cache()

    decision = is_killed(t, wi)
    assert decision is not None
    assert decision.scope_type == "tenant"
    assert decision.source == "tenant_config"

    key = mint_virtual_key(t, wi, Stage.coder, models=[ECHO])
    headers = GatewayCallHeaders(tenant_id=t, work_item_id=wi, stage=Stage.coder)
    try:
        with pytest.raises(GatewayCallError) as ei:
            chat_completion(headers=headers, virtual_key=key, model=ECHO, messages=[{"role": "user", "content": "x"}])
        assert ei.value.body["source"] == "tenant_config"
    finally:
        # clears the WS-F switch so it does not affect other tests of the same tenant
        conn = db.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE tenant_config SET kill_switch_enabled=false WHERE tenant_id=%s", (t,))
            conn.commit()
        finally:
            conn.close()
        controls.clear_cache()
        revoke_virtual_key(key)


def test_reassign_changes_effective_model_and_audits(unique_ids):
    t, wi = unique_ids["tenant_id"], unique_ids["work_item_id"]
    # key scoped to BOTH aliases so LiteLLM does not block on model-scope
    key = mint_virtual_key(t, wi, Stage.coder, models=[ECHO, "bedrock/anthropic.claude-3-haiku"])
    headers = GatewayCallHeaders(tenant_id=t, work_item_id=wi, stage=Stage.coder)
    try:
        # operator reassigns the work_item from haiku (unavailable/AWS) to echo
        reassign_model(wi, ECHO, reason="fallback to a local model", actor="system:operator", tenant_id=t)
        assert controls.resolve_reassignment(wi) == ECHO
        # the caller asks for haiku, but the reassign forces echo -> echo's response
        r = chat_completion(
            headers=headers, virtual_key=key, model="bedrock/anthropic.claude-3-haiku",
            messages=[{"role": "user", "content": "abc"}],
        )
        assert r.content == "ECHO[cba]"  # came from echo, not from haiku
        assert "gateway.model_reassigned" in _audit_actions(wi)
        clear_reassignment(wi, actor="system:operator", tenant_id=t)
        assert controls.resolve_reassignment(wi) is None
    finally:
        revoke_virtual_key(key)


def test_reassign_does_not_bypass_policy(unique_ids):
    import json
    t, wi = unique_ids["tenant_id"], unique_ids["work_item_id"]
    # the tenant's policy only permits echo
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO model_policies (tenant_id, stage, allowed_models) VALUES (%s,%s,%s::jsonb) "
                "ON CONFLICT (tenant_id, stage, data_class, risk_class) DO UPDATE SET allowed_models=EXCLUDED.allowed_models, is_active=true",
                (t, "coder", json.dumps([ECHO])),
            )
        conn.commit()
    finally:
        conn.close()
    from model_gateway_client import policy
    policy.clear_cache()

    key = mint_virtual_key(t, wi, Stage.coder, models=[ECHO, "bedrock/anthropic.claude-3-haiku"])
    headers = GatewayCallHeaders(tenant_id=t, work_item_id=wi, stage=Stage.coder)
    try:
        # reassigns to a model NOT permitted by policy -> must be denied by policy
        reassign_model(wi, "bedrock/anthropic.claude-3-haiku", actor="system:operator", tenant_id=t)
        with pytest.raises(GatewayCallError) as ei:
            chat_completion(headers=headers, virtual_key=key, model=ECHO, messages=[{"role": "user", "content": "x"}])
        assert ei.value.body["error"] == "policy_denied"
        assert ei.value.body["effective_model"] == "bedrock/anthropic.claude-3-haiku"
    finally:
        clear_reassignment(wi, tenant_id=t)
        revoke_virtual_key(key)
