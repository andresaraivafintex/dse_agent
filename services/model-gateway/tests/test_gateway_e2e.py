"""Fim-a-fim contra o LiteLLM proxy REAL (docker-compose.wsd.yml) + o modelo
eco real. Nenhum mock de HTTP/Postgres — a garantia de durabilidade
(virtual key emitida de verdade, revogada de verdade, chamada negada de
verdade após revoke) é o próprio ponto do teste (P8: evidência > asserção).

Requer a infra do WS-D no ar:
    docker compose -f docker-compose.wsd.yml up -d
"""
from __future__ import annotations

import pytest
from dse_contracts.gateway_contract import GatewayCallHeaders, Stage
from model_gateway_client import chat_completion, mint_virtual_key, revoke_virtual_key
from model_gateway_client.errors import GatewayCallError

from .conftest import ECHO_MODEL


def test_mint_call_revoke_denied_roundtrip(unique_ids):
    tenant_id = unique_ids["tenant_id"]
    work_item_id = unique_ids["work_item_id"]

    key = mint_virtual_key(tenant_id, work_item_id, Stage.coder, models=[ECHO_MODEL])
    assert key.startswith("sk-")

    headers = GatewayCallHeaders(
        tenant_id=tenant_id, work_item_id=work_item_id, stage=Stage.coder, task_class="unit-test"
    )
    result = chat_completion(
        headers=headers,
        virtual_key=key,
        model=ECHO_MODEL,
        messages=[{"role": "user", "content": "hello dse gateway"}],
    )
    assert result.content == "ECHO[yawetag esd olleh]"
    assert result.tokens_in == 3
    assert result.tokens_out == 3
    assert result.cost_usd >= 0.0

    revoke_virtual_key(key)

    with pytest.raises(GatewayCallError) as excinfo:
        chat_completion(
            headers=headers,
            virtual_key=key,
            model=ECHO_MODEL,
            messages=[{"role": "user", "content": "should be denied now"}],
        )
    assert excinfo.value.status_code == 401


def test_virtual_key_is_scoped_to_allowed_models(unique_ids):
    """Uma virtual key emitida só com `models=[ECHO_MODEL]` não deve
    conseguir chamar um model_name diferente registrado no gateway."""
    key = mint_virtual_key(
        unique_ids["tenant_id"], unique_ids["work_item_id"], Stage.coder, models=[ECHO_MODEL]
    )
    headers = GatewayCallHeaders(
        tenant_id=unique_ids["tenant_id"],
        work_item_id=unique_ids["work_item_id"],
        stage=Stage.coder,
    )
    try:
        with pytest.raises(GatewayCallError) as excinfo:
            chat_completion(
                headers=headers,
                virtual_key=key,
                model="bedrock/anthropic.claude-3-haiku",
                messages=[{"role": "user", "content": "x"}],
            )
        assert excinfo.value.status_code in (401, 403, 400)
    finally:
        revoke_virtual_key(key)


def test_revoke_unknown_key_raises_clean_error():
    from model_gateway_client.errors import VirtualKeyNotFoundError

    with pytest.raises(VirtualKeyNotFoundError):
        revoke_virtual_key("sk-this-key-was-never-minted-by-us")
