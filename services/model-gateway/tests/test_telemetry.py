"""WSD-E3-T1: every model call produces an OTel span with the
`dse_contracts.constants.OTEL_ATTR_*` attributes, filled from LiteLLM's REAL
response (cost/tokens are not recomputed by us)."""
from __future__ import annotations

from dse_contracts.constants import (
    OTEL_ATTR_COST_USD,
    OTEL_ATTR_MODEL,
    OTEL_ATTR_STAGE,
    OTEL_ATTR_TENANT,
    OTEL_ATTR_TOKENS_IN,
    OTEL_ATTR_TOKENS_OUT,
    OTEL_ATTR_WORK_ITEM,
)
from dse_contracts.gateway_contract import GatewayCallHeaders, Stage
from model_gateway_client import chat_completion, mint_virtual_key, revoke_virtual_key
from model_gateway_client.telemetry import OTEL_ATTR_TASK_CLASS, get_recorded_spans


def test_chat_completion_emits_span_with_contract_attributes(unique_ids):
    tenant_id = unique_ids["tenant_id"]
    work_item_id = unique_ids["work_item_id"]
    key = mint_virtual_key(tenant_id, work_item_id, Stage.coder, models=["eco/echo-model"])
    try:
        headers = GatewayCallHeaders(
            tenant_id=tenant_id, work_item_id=work_item_id, stage=Stage.coder, task_class="otel-test"
        )
        result = chat_completion(
            headers=headers,
            virtual_key=key,
            model="eco/echo-model",
            messages=[{"role": "user", "content": "trace me"}],
        )

        spans = [
            s
            for s in get_recorded_spans()
            if s.attributes.get(OTEL_ATTR_WORK_ITEM) == work_item_id
        ]
        assert len(spans) == 1
        span = spans[0]
        assert span.name == "dse.model_gateway.chat_completion"
        attrs = span.attributes
        assert attrs[OTEL_ATTR_TENANT] == tenant_id
        assert attrs[OTEL_ATTR_WORK_ITEM] == work_item_id
        assert attrs[OTEL_ATTR_STAGE] == "coder"
        assert attrs[OTEL_ATTR_MODEL] == result.model
        assert attrs[OTEL_ATTR_TASK_CLASS] == "otel-test"
        assert attrs[OTEL_ATTR_TOKENS_IN] == result.tokens_in
        assert attrs[OTEL_ATTR_TOKENS_OUT] == result.tokens_out
        assert attrs[OTEL_ATTR_COST_USD] == result.cost_usd
    finally:
        revoke_virtual_key(key)


def test_span_recorded_even_on_gateway_error(unique_ids):
    """Errors must stay traceable too (span with ERROR status), not just the
    happy path — otherwise denial observability is blind."""
    from model_gateway_client.errors import GatewayCallError

    tenant_id = unique_ids["tenant_id"]
    work_item_id = unique_ids["work_item_id"]
    key = mint_virtual_key(tenant_id, work_item_id, Stage.coder, models=["eco/echo-model"])
    revoke_virtual_key(key)  # makes the key invalid on purpose

    headers = GatewayCallHeaders(tenant_id=tenant_id, work_item_id=work_item_id, stage=Stage.coder)
    import pytest

    with pytest.raises(GatewayCallError):
        chat_completion(
            headers=headers,
            virtual_key=key,
            model="eco/echo-model",
            messages=[{"role": "user", "content": "denied"}],
        )

    spans = [s for s in get_recorded_spans() if s.attributes.get(OTEL_ATTR_WORK_ITEM) == work_item_id]
    assert len(spans) == 1
    assert spans[0].status.status_code.name == "ERROR"
