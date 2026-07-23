"""Boundary tests do contrato de turno isolado (plano 09, Fase 1).

Mesma disciplina de `test_activity_boundaries`: payloads LITERAIS (o que de
fato atravessa a fronteira worker → agent-runner), nunca objetos construídos
com defaults — se um campo mudar de nome/shape, este arquivo quebra JUNTO.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from dse_contracts import (
    AGENT_TURN_SCHEMA_VERSION,
    AgentTurnRequest,
    AgentTurnResult,
    KNOWN_SUBSTRATES,
)


def test_request_literal_payload_roundtrip():
    payload = {
        "schema_version": 1,
        "work_item_id": "wi_123",
        "tenant_id": "tenant_dev",
        "stage": "coder",
        "substrate": "claude-agent",
        "instruction": "Implement the handler.",
        "model": "gateway/coder-default",
        "allowed_tools": ["Read", "Write", "Edit", "Glob", "Grep"],
        "workspace_dir": "/workspace",
        "timeout_seconds": 900.0,
        "gateway": {
            "base_url": "http://model-gateway:4000",
            "virtual_key": "vk-ephemeral-abc",
            "headers": {"X-Dse-Tenant-Id": "tenant_dev", "X-Dse-Work-Item-Id": "wi_123"},
        },
    }
    req = AgentTurnRequest.model_validate(payload)
    assert req.gateway.virtual_key == "vk-ephemeral-abc"
    # roundtrip estável: o que o worker serializa é o que o runner decodifica
    assert AgentTurnRequest.model_validate(req.model_dump()) == req


def test_request_minimal_payload_uses_safe_defaults():
    req = AgentTurnRequest.model_validate(
        {
            "work_item_id": "wi_1",
            "tenant_id": "t",
            "stage": "coder",
            "substrate": "fake",
            "instruction": "x",
            "gateway": {"base_url": "http://gw:4000", "virtual_key": "vk"},
        }
    )
    assert req.schema_version == AGENT_TURN_SCHEMA_VERSION
    assert req.workspace_dir == "/workspace"  # nunca path do host
    assert "Bash" not in req.allowed_tools  # toolset só de edição (P1)
    assert req.fake_script is None


def test_request_rejects_unknown_field():
    with pytest.raises(ValidationError):
        AgentTurnRequest.model_validate(
            {
                "work_item_id": "wi_1",
                "tenant_id": "t",
                "stage": "coder",
                "substrate": "fake",
                "instruction": "x",
                "gateway": {"base_url": "u", "virtual_key": "vk"},
                "host_workspace_path": "/Users/alguem/repo",  # NUNCA atravessa
            }
        )


def test_result_literal_payload_and_error_vocabulary():
    ok = AgentTurnResult.model_validate(
        {
            "schema_version": 1,
            "done": True,
            "thoughts": ["editei o arquivo"],
            "tool_calls": ["Edit"],
            "cost_usd": 0.0123,
            "tokens_in": 100,
            "tokens_out": 50,
        }
    )
    assert not ok.failed

    err = AgentTurnResult.model_validate(
        {"done": False, "error": "openhands ainda não roda no runner", "error_kind": "unsupported_substrate"}
    )
    assert err.failed


def test_known_substrates_vocabulary_is_closed():
    # O runner e o runtime_profile dependem deste vocabulário; mudança aqui
    # exige mudar os dois lados no MESMO PR.
    assert KNOWN_SUBSTRATES == ("fake", "claude-agent", "openhands")
