"""WSC-E3-T1: interface de substrato + adapters.

`test_openhands_substrate_wiring` prova que a construção real do
`openhands-sdk` aponta SEMPRE para o model-gateway (nunca para um SDK de
provider) — sem exercitar um turno completo (que exigiria o model-gateway
do WS-D de pé + inferência real, fora do escopo desta suíte; ver README)."""
from __future__ import annotations

from pathlib import Path

from dse_contracts import GatewayCallHeaders, Stage
from sandbox_runtime.substrate import AgentSubstrate, FakeSubstrate


def test_fake_substrate_conforms_to_agent_substrate_protocol():
    fake = FakeSubstrate([])
    assert isinstance(fake, AgentSubstrate)


def test_fake_substrate_applies_scripted_edits_deterministically(tmp_path):
    headers = GatewayCallHeaders(tenant_id="t1", work_item_id="wi1", stage=Stage.coder)
    script = [
        {"write_files": {"a.py": "x = 1\n"}, "cost_usd": 0.02, "tokens_in": 10, "tokens_out": 5, "done": False},
        {"write_files": {"b.py": "y = 2\n"}, "cost_usd": 0.03, "tokens_in": 20, "tokens_out": 8, "done": True},
    ]
    fake = FakeSubstrate(script)
    fake.create_session(
        work_item_id="wi1",
        workspace_dir=str(tmp_path),
        gateway_headers=headers,
        virtual_key="vk-test",
        gateway_base_url="http://localhost:4000",
    )

    log1 = fake.run_turn("passo 1")
    assert log1.done is False
    log2 = fake.run_turn("passo 2")
    assert log2.done is True

    assert (tmp_path / "a.py").read_text() == "x = 1\n"
    assert (tmp_path / "b.py").read_text() == "y = 2\n"

    artifacts = fake.collect_artifacts()
    assert set(artifacts.files_changed) == {"a.py", "b.py"}
    assert artifacts.cost_usd == 0.05
    assert artifacts.tokens_in == 30
    assert artifacts.tokens_out == 13


def test_openhands_substrate_wiring_routes_through_model_gateway_only():
    try:
        import openhands.sdk  # noqa: F401
    except ImportError:
        import pytest

        pytest.skip("openhands-sdk não instalado neste venv")

    from sandbox_runtime.substrate import OpenHandsSubstrate

    headers = GatewayCallHeaders(tenant_id="t1", work_item_id="wi1", stage=Stage.coder)
    substrate = OpenHandsSubstrate(model="gateway/coder-default")
    substrate.create_session(
        work_item_id="wi1",
        workspace_dir="/tmp",
        gateway_headers=headers,
        virtual_key="vk-test-not-a-real-secret",
        gateway_base_url="http://localhost:4000",
    )

    # O LLM real do OpenHands SDK tem que apontar pro model-gateway, nunca
    # para um provider (api.anthropic.com/api.openai.com/bedrock-runtime.*).
    assert substrate._llm.base_url == "http://localhost:4000"
    assert substrate._llm.api_key.get_secret_value() == "vk-test-not-a-real-secret" if hasattr(
        substrate._llm.api_key, "get_secret_value"
    ) else substrate._llm.api_key == "vk-test-not-a-real-secret"
    assert substrate._llm.extra_headers == headers.to_http_headers()
