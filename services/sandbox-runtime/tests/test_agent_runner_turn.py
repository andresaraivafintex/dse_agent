"""Fase 1 (plano 09): o agent-runner executa o turno de verdade no sandbox.

Estes testes rodam o executor EM PROCESSO (sem docker/k8s): provam o contrato
(decodificação com o modelo real, envelope dos drivers, vocabulário de erro) e
a semântica do substrato fake — a mesma que a suíte de conformidade usa. O
turno claude-agent vivo (inferência real) continua fora da suíte, como todo
substrato real (ver README "o que falta para produção").
"""
from __future__ import annotations

import io
import json
import os
import sys

# O pacote agent_runner vive na imagem do sandbox (services/sandbox-runtime/
# agent-runner), não no venv do worker — os testes o importam pelo caminho.
_RUNNER_DIR = os.path.join(os.path.dirname(__file__), "..", "agent-runner")
sys.path.insert(0, os.path.abspath(_RUNNER_DIR))

from agent_runner.__main__ import main as runner_main  # noqa: E402
from agent_runner.executor import build_claude_gateway_env, run_agent_turn  # noqa: E402
from dse_contracts import AgentTurnRequest  # noqa: E402


def _req(tmp_path, **overrides):
    base = {
        "work_item_id": "wi_t1",
        "tenant_id": "tenant-a",
        "stage": "coder",
        "substrate": "fake",
        "instruction": "implemente",
        "workspace_dir": str(tmp_path),
        "gateway": {"base_url": "http://model-gateway:4000", "virtual_key": "vk-eph"},
    }
    base.update(overrides)
    return AgentTurnRequest.model_validate(base)


def test_fake_substrate_writes_files_inside_workspace(tmp_path):
    req = _req(
        tmp_path,
        fake_script=[
            {"write_files": {"src/handler.py": "def handler():\n    return 'ok'\n"},
             "thought": "implementa handler", "done": True},
        ],
    )
    result = run_agent_turn(req)
    assert result.done and not result.failed
    assert (tmp_path / "src" / "handler.py").read_text().startswith("def handler")
    assert result.thoughts == ["implementa handler"]


def test_openhands_missing_sdk_is_structured_error_not_fallback(tmp_path, monkeypatch):
    # força ImportError mesmo se o venv tiver o SDK (sys.modules[name] = None)
    monkeypatch.setitem(sys.modules, "openhands", None)
    monkeypatch.setitem(sys.modules, "openhands.sdk", None)
    result = run_agent_turn(_req(tmp_path, substrate="openhands"))
    assert result.failed and result.error_kind == "substrate_error"
    assert "INSTALL_OPENHANDS" in (result.error or "")
    assert not result.done


def test_openhands_wiring_is_gateway_only(tmp_path, monkeypatch):
    """Stub do openhands.sdk: prova que o LLM aponta para o gateway com a
    virtual key + headers do contrato e o workspace é o do request — dentro
    do sandbox, LocalWorkspace é o desenho certo."""
    import types

    seen: dict = {}

    class _LLM:
        def __init__(self, **kw):
            seen["llm"] = kw

    class _Agent:
        def __init__(self, llm):
            seen["agent_llm"] = llm

    class _LocalWorkspace:
        def __init__(self, working_dir):
            seen["working_dir"] = working_dir

    class _Conversation:
        conversation_stats = types.SimpleNamespace(
            total_cost_usd=0.25, total_tokens_in=11, total_tokens_out=7
        )

        def __init__(self, agent, workspace):
            pass

        def send_message(self, msg):
            seen["message"] = msg

        def run(self):
            seen["ran"] = True

    sdk = types.ModuleType("openhands.sdk")
    sdk.LLM, sdk.Agent, sdk.Conversation, sdk.LocalWorkspace = _LLM, _Agent, _Conversation, _LocalWorkspace
    pkg = types.ModuleType("openhands")
    pkg.sdk = sdk
    monkeypatch.setitem(sys.modules, "openhands", pkg)
    monkeypatch.setitem(sys.modules, "openhands.sdk", sdk)

    req = _req(
        tmp_path, substrate="openhands", model="gateway/coder-default",
        gateway={"base_url": "http://model-gateway:4000", "virtual_key": "vk-oh",
                 "headers": {"X-Dse-Stage": "coder"}},
    )
    result = run_agent_turn(req)
    assert not result.failed and result.done and seen["ran"]
    assert seen["llm"]["base_url"] == "http://model-gateway:4000"
    assert seen["llm"]["api_key"] == "vk-oh"
    assert seen["llm"]["extra_headers"] == {"X-Dse-Stage": "coder"}
    assert seen["working_dir"] == str(tmp_path)
    assert result.cost_usd == 0.25 and result.tokens_in == 11 and result.tokens_out == 7


def test_unknown_substrate_is_clean_error(tmp_path):
    result = run_agent_turn(_req(tmp_path, substrate="banana"))
    assert result.error_kind == "unsupported_substrate"


def test_claude_gateway_env_is_gateway_only(tmp_path):
    req = _req(
        tmp_path,
        substrate="claude-agent",
        gateway={
            "base_url": "http://model-gateway:4000",
            "virtual_key": "vk-eph",
            "headers": {"X-Dse-Tenant-Id": "tenant-a", "X-Dse-Stage": "coder"},
        },
    )
    env = build_claude_gateway_env(req)
    assert env["ANTHROPIC_BASE_URL"] == "http://model-gateway:4000"
    assert env["ANTHROPIC_API_KEY"] == "vk-eph"  # SÓ a virtual key efêmera
    assert "X-Dse-Tenant-Id: tenant-a" in env["ANTHROPIC_CUSTOM_HEADERS"]
    # nenhuma credencial de provider/master key existe neste dicionário
    assert not any("MASTER" in k or "AWS" in k for k in env)


def _run_main_with_stdin(payload: str) -> tuple[int, dict]:
    old_in, old_out = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = io.StringIO(payload), io.StringIO()
    try:
        code = runner_main(["--stage", "coder"])
        out = sys.stdout.getvalue()
    finally:
        sys.stdin, sys.stdout = old_in, old_out
    return code, json.loads(out.strip().splitlines()[-1])


def test_main_accepts_driver_envelope_and_raw_request(tmp_path):
    req = _req(tmp_path, fake_script=[{"write_files": {"a.txt": "x"}, "done": True}])
    # envelope dos drivers ({"stage","input"}) — formato do kubectl/docker exec
    code, out = _run_main_with_stdin(json.dumps({"stage": "coder", "input": req.model_dump()}))
    assert code == 0 and out["done"] is True and out["error_kind"] is None
    # request cru também é aceito (mesmo contrato)
    code, out = _run_main_with_stdin(req.model_dump_json())
    assert code == 0 and out["done"] is True


def test_main_invalid_payload_is_structured_error(tmp_path):
    code, out = _run_main_with_stdin(json.dumps({"input": {"host_workspace_path": "/Users/x"}}))
    assert code == 0
    assert out["error_kind"] == "invalid_payload" and out["done"] is False
    # stdin que nem é JSON → exit != 0 (falha catastrófica, driver traduz)
    code, out = _run_main_with_stdin("isto não é json")
    assert code == 2
