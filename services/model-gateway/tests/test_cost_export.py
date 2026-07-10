"""WSD-E3-T2: agregação de custo por tenant/task-class/stage a partir dos
spans emitidos por chamadas reais ao gateway."""
from __future__ import annotations

from dse_contracts.gateway_contract import GatewayCallHeaders, Stage
from model_gateway_client import chat_completion, mint_virtual_key, revoke_virtual_key
from model_gateway_client.cost_export import aggregate_cost, aggregate_cost_by_tenant


def test_aggregate_cost_groups_by_tenant_task_class_stage(unique_ids):
    tenant_id = unique_ids["tenant_id"]
    work_item_id = unique_ids["work_item_id"]
    key = mint_virtual_key(tenant_id, work_item_id, Stage.coder, models=["eco/echo-model"])
    try:
        headers_a = GatewayCallHeaders(
            tenant_id=tenant_id, work_item_id=work_item_id, stage=Stage.coder, task_class="feature"
        )
        headers_b = GatewayCallHeaders(
            tenant_id=tenant_id, work_item_id=work_item_id, stage=Stage.coder, task_class="bugfix"
        )

        chat_completion(headers=headers_a, virtual_key=key, model="eco/echo-model", messages=[{"role": "user", "content": "one two"}])
        chat_completion(headers=headers_a, virtual_key=key, model="eco/echo-model", messages=[{"role": "user", "content": "three four five"}])
        chat_completion(headers=headers_b, virtual_key=key, model="eco/echo-model", messages=[{"role": "user", "content": "x"}])

        rows = aggregate_cost(tenant_id=tenant_id)
        by_task_class = {r["task_class"]: r for r in rows}

        assert by_task_class["feature"]["call_count"] == 2
        assert by_task_class["feature"]["total_tokens_in"] == 2 + 3  # "one two" / "three four five"
        assert by_task_class["bugfix"]["call_count"] == 1
        assert by_task_class["bugfix"]["stage"] == "coder"
        assert "eco/echo-model" in by_task_class["feature"]["models"]
    finally:
        revoke_virtual_key(key)


def test_aggregate_cost_filters_by_tenant(unique_ids):
    tenant_a = unique_ids["tenant_id"]
    tenant_b = f"{tenant_a}-other"
    work_item_id = unique_ids["work_item_id"]

    key_a = mint_virtual_key(tenant_a, work_item_id, Stage.coder, models=["eco/echo-model"])
    key_b = mint_virtual_key(tenant_b, work_item_id, Stage.coder, models=["eco/echo-model"])
    try:
        chat_completion(
            headers=GatewayCallHeaders(tenant_id=tenant_a, work_item_id=work_item_id, stage=Stage.coder),
            virtual_key=key_a,
            model="eco/echo-model",
            messages=[{"role": "user", "content": "a"}],
        )
        chat_completion(
            headers=GatewayCallHeaders(tenant_id=tenant_b, work_item_id=work_item_id, stage=Stage.coder),
            virtual_key=key_b,
            model="eco/echo-model",
            messages=[{"role": "user", "content": "b"}],
        )

        rows_a = aggregate_cost(tenant_id=tenant_a)
        assert all(r["tenant_id"] == tenant_a for r in rows_a)
        assert len(rows_a) == 1

        totals = aggregate_cost_by_tenant()
        assert tenant_a in totals
        assert tenant_b in totals
    finally:
        revoke_virtual_key(key_a)
        revoke_virtual_key(key_b)
