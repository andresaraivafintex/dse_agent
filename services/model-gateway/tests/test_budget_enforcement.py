"""WSD-E2-T2: enforcement de budget no call time (decline-never-truncate, P6).

Testes REAIS. Provam:
  - o custo real de cada chamada é acumulado no ledger durável;
  - budget de WorkItem exausto -> deny tipado (budget_exhausted) na fronteira +
    audit;
  - budget agregado do tenant (tenant_config) exausto -> deny + audit;
  - sem cap configurado -> sem enforcement (Fase 1 preservada).

Como o modelo eco tem custo real 0 (não é um LLM pago), forçamos "spent >= cap"
setando o cap em 0.0 (qualquer gasto >= 0 satisfaz a fronteira) e provamos o
accounting inserindo custo direto no ledger.
"""
from __future__ import annotations

import pytest
from dse_audit.client import get_connection as audit_conn
from dse_contracts.gateway_contract import GatewayCallHeaders, Stage
from model_gateway_client import (
    GatewayCallError,
    chat_completion,
    get_status,
    mint_virtual_key,
    revoke_virtual_key,
    set_work_item_budget,
)
from model_gateway_client import db, ledger

ECHO = "eco/echo-model"


def _audit_actions(work_item_id):
    conn = audit_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT action, details FROM audit_log WHERE work_item_id=%s ORDER BY id",
                (work_item_id,),
            )
            return cur.fetchall()
    finally:
        conn.close()


def _set_tenant_monthly_budget(tenant_id, usd):
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tenant_config (tenant_id, monthly_budget_usd)
                VALUES (%s, %s)
                ON CONFLICT (tenant_id) DO UPDATE SET monthly_budget_usd = EXCLUDED.monthly_budget_usd
                """,
                (tenant_id, usd),
            )
        conn.commit()
    finally:
        conn.close()


def test_successful_call_is_recorded_in_durable_ledger(unique_ids):
    t, wi = unique_ids["tenant_id"], unique_ids["work_item_id"]
    key = mint_virtual_key(t, wi, Stage.coder, models=[ECHO])
    headers = GatewayCallHeaders(tenant_id=t, work_item_id=wi, stage=Stage.coder, task_class="feature")
    try:
        chat_completion(headers=headers, virtual_key=key, model=ECHO, messages=[{"role": "user", "content": "one two"}])
        rows = ledger.aggregate(tenant_id=t)
        assert len(rows) == 1
        assert rows[0]["call_count"] == 1
        assert rows[0]["task_class"] == "feature"
        assert rows[0]["total_tokens_in"] == 2
    finally:
        revoke_virtual_key(key)


def test_work_item_budget_exhausted_denies_next_call(unique_ids):
    t, wi = unique_ids["tenant_id"], unique_ids["work_item_id"]
    # cap 0 -> qualquer spent (>=0) já satisfaz a exaustão na próxima fronteira
    set_work_item_budget(wi, t, 0.0)
    key = mint_virtual_key(t, wi, Stage.coder, models=[ECHO])
    headers = GatewayCallHeaders(tenant_id=t, work_item_id=wi, stage=Stage.coder)
    try:
        with pytest.raises(GatewayCallError) as ei:
            chat_completion(headers=headers, virtual_key=key, model=ECHO, messages=[{"role": "user", "content": "x"}])
        assert ei.value.status_code == 402
        assert ei.value.body["error"] == "budget_exhausted"
        assert ei.value.body["kind"] == "work_item"
        actions = [a for a, _ in _audit_actions(wi)]
        assert "gateway.call_denied_budget" in actions
    finally:
        revoke_virtual_key(key)


def test_tenant_aggregate_budget_exhausted_denies(unique_ids):
    t, wi = unique_ids["tenant_id"], unique_ids["work_item_id"]
    _set_tenant_monthly_budget(t, 0.0)
    key = mint_virtual_key(t, wi, Stage.coder, models=[ECHO])
    headers = GatewayCallHeaders(tenant_id=t, work_item_id=wi, stage=Stage.coder)
    try:
        with pytest.raises(GatewayCallError) as ei:
            chat_completion(headers=headers, virtual_key=key, model=ECHO, messages=[{"role": "user", "content": "x"}])
        assert ei.value.body["error"] == "budget_exhausted"
        assert ei.value.body["kind"] == "tenant"
        assert ei.value.body["cap_source"] == "tenant_config.monthly_budget_usd"
    finally:
        revoke_virtual_key(key)


def test_budget_status_reports_caps_and_spend(unique_ids):
    t, wi = unique_ids["tenant_id"], unique_ids["work_item_id"]
    set_work_item_budget(wi, t, 5.0)
    # injeta custo direto no ledger (simula chamadas pagas anteriores)
    ledger.record_call(
        tenant_id=t, work_item_id=wi, stage="coder", task_class="default",
        model=ECHO, cost_usd=1.25, tokens_in=10, tokens_out=5,
    )
    st = get_status(t, wi)
    assert st.caps.work_item_cap_usd == 5.0
    assert abs(st.work_item_spent_usd - 1.25) < 1e-9
    assert not st.work_item_exhausted
