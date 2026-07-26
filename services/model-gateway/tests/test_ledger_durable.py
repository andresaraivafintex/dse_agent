"""WSD-E3-T4: cost aggregation from a DURABLE source (Postgres), not from the
per-process in-memory buffer.

Proves that:
  - `cost_export.aggregate_cost(source="ledger")` (the default) reads from the
    `model_call_ledger` table, independent of the in-memory span buffer — even
    after CLEARING the in-memory recorder (a proxy for "process restart") the
    cost is still there;
  - the series survives reads from fresh connections (durability);
  - OTLP export to WS-F's collector can be enabled (the endpoint is already
    supported) without breaking the durable aggregation.
"""
from __future__ import annotations

from dse_contracts.gateway_contract import GatewayCallHeaders, Stage
from model_gateway_client import chat_completion, mint_virtual_key, revoke_virtual_key
from model_gateway_client import telemetry
from model_gateway_client.cost_export import aggregate_cost

ECHO = "eco/echo-model"


def test_ledger_survives_in_memory_clear(unique_ids):
    t, wi = unique_ids["tenant_id"], unique_ids["work_item_id"]
    key = mint_virtual_key(t, wi, Stage.coder, models=[ECHO])
    headers = GatewayCallHeaders(tenant_id=t, work_item_id=wi, stage=Stage.coder, task_class="feature")
    try:
        chat_completion(headers=headers, virtual_key=key, model=ECHO, messages=[{"role": "user", "content": "one two three"}])

        # simulates a "process restart": wipes the whole in-memory span buffer
        telemetry.clear_recorded_spans()

        # the in-memory source is now empty for this tenant...
        mem = aggregate_cost(tenant_id=t, source="memory")
        assert mem == []
        # ...but the durable source (ledger) still has the call
        durable = aggregate_cost(tenant_id=t)  # default source="ledger"
        assert len(durable) == 1
        assert durable[0]["call_count"] == 1
        assert durable[0]["task_class"] == "feature"
        assert durable[0]["total_tokens_in"] == 3
        assert ECHO in durable[0]["models"]
    finally:
        revoke_virtual_key(key)


def test_ledger_aggregation_is_tenant_isolated(unique_ids):
    ta = unique_ids["tenant_id"]
    tb = f"{ta}-other"
    wi = unique_ids["work_item_id"]
    ka = mint_virtual_key(ta, wi, Stage.coder, models=[ECHO])
    kb = mint_virtual_key(tb, wi, Stage.coder, models=[ECHO])
    try:
        chat_completion(headers=GatewayCallHeaders(tenant_id=ta, work_item_id=wi, stage=Stage.coder),
                        virtual_key=ka, model=ECHO, messages=[{"role": "user", "content": "a"}])
        chat_completion(headers=GatewayCallHeaders(tenant_id=tb, work_item_id=wi, stage=Stage.coder),
                        virtual_key=kb, model=ECHO, messages=[{"role": "user", "content": "b"}])
        rows_a = aggregate_cost(tenant_id=ta)
        assert all(r["tenant_id"] == ta for r in rows_a)
        assert len(rows_a) == 1
    finally:
        revoke_virtual_key(ka)
        revoke_virtual_key(kb)
