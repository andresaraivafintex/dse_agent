"""Boundary tests of the isolated-turn contract (plano 09, Phase 1).

Same discipline as `test_activity_boundaries`: LITERAL payloads (what actually
crosses the worker → agent-runner boundary), never objects built with defaults
— if a field changes name/shape, this file breaks ALONG WITH it.
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
    # stable roundtrip: what the worker serializes is what the runner decodes
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
    assert req.workspace_dir == "/workspace"  # never a host path
    assert "Bash" not in req.allowed_tools  # edit-only toolset (P1)
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
                "host_workspace_path": "/Users/alguem/repo",  # NEVER crosses
            }
        )


def test_result_literal_payload_and_error_vocabulary():
    ok = AgentTurnResult.model_validate(
        {
            "schema_version": 1,
            "done": True,
            "thoughts": ["edited the file"],
            "tool_calls": ["Edit"],
            "cost_usd": 0.0123,
            "tokens_in": 100,
            "tokens_out": 50,
        }
    )
    assert not ok.failed

    err = AgentTurnResult.model_validate(
        {"done": False, "error": "openhands does not run on the runner yet", "error_kind": "unsupported_substrate"}
    )
    assert err.failed


def test_known_substrates_vocabulary_is_closed():
    # The runner and the runtime_profile depend on this vocabulary; a change
    # here requires changing both sides in the SAME PR.
    assert KNOWN_SUBSTRATES == ("fake", "claude-agent", "openhands")
