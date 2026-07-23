"""Fase 1 (plano 09): RemoteSubstrate — o worker despacha o contrato tipado.

Prova, sem docker/k8s (StubDriver), as propriedades da fronteira:
  - nenhum path do host atravessa (workspace do request é /workspace);
  - só a virtual key efêmera viaja (nunca master key);
  - falha do runner vira RemoteTurnError com kind fechado (sem substring);
  - a seleção in-process ↔ isolado é config de deployment
    (`DSE_SANDBOX_INPROCESS`), sem mudar código de workflow;
  - o pipeline determinístico completo do Coder (prune → revert de testes →
    commit/push escopado) funciona com o turno executado "à distância".
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from dse_contracts import AgentTurnResult, RunCoderTurnInput, Stage

from sandbox_runtime.activities import (
    ProvisionSandboxInput,
    _build_substrate,
    _paths_for,
    _run_coder_turn_impl,
    provision_sandbox,
    teardown_sandbox,
)
from sandbox_runtime.driver import StageExecutionRequest, StageExecutionResult
from sandbox_runtime.remote_substrate import RemoteSubstrate, RemoteTurnError
from sandbox_runtime.substrate import FakeSubstrate

from dse_contracts import GatewayCallHeaders


@dataclass
class StubDriver:
    """SandboxDriver mínimo: grava o request e devolve um resultado roteirizado.
    `write_into` simula o efeito do bind mount: o runner editou /workspace e o
    worker enxerga a edição no path do host."""

    result: dict[str, Any] = field(default_factory=lambda: AgentTurnResult(done=True).model_dump())
    write_into: str | None = None
    write_files: dict[str, str] = field(default_factory=dict)
    requests: list[StageExecutionRequest] = field(default_factory=list)

    @property
    def supports_isolated_stage_execution(self) -> bool:
        return True

    def sandbox_id_for(self, work_item_id: str) -> str:
        return f"stub-sbx-{work_item_id}"

    def execute_stage(self, request: StageExecutionRequest) -> StageExecutionResult:
        self.requests.append(request)
        if self.write_into:
            import os

            for rel, content in self.write_files.items():
                dest = os.path.join(self.write_into, rel)
                os.makedirs(os.path.dirname(dest) or self.write_into, exist_ok=True)
                with open(dest, "w") as fh:
                    fh.write(content)
        return StageExecutionResult(
            stage=request.stage, output_payload=self.result, exit_code=0, duration_seconds=0.01
        )


def _headers(wi: str = "wi_r1") -> GatewayCallHeaders:
    return GatewayCallHeaders(
        tenant_id="tenant-a", work_item_id=wi, stage=Stage.coder,
        task_class="feature", data_class="internal",
    )


def _session(sub: RemoteSubstrate, wi: str = "wi_r1", workspace: str = "/tmp/host-ws") -> None:
    sub.create_session(
        work_item_id=wi,
        workspace_dir=workspace,
        gateway_headers=_headers(wi),
        virtual_key="vk-ephemeral",
        gateway_base_url="http://localhost:4000",
    )


def test_request_never_carries_host_paths_or_master_key(tmp_path):
    driver = StubDriver()
    sub = RemoteSubstrate(driver=driver, substrate_name="fake", fake_script=[{"done": True}])
    _session(sub, workspace=str(tmp_path))

    sub.run_turn("implemente o handler")

    assert len(driver.requests) == 1
    payload = driver.requests[0].input_payload
    assert payload["workspace_dir"] == "/workspace"  # nunca o path do host
    assert str(tmp_path) not in str(payload)
    assert payload["gateway"]["virtual_key"] == "vk-ephemeral"
    assert payload["fake_script"] == [{"done": True}]
    assert "X-Dse-Work-Item-Id" in payload["gateway"]["headers"]
    # margem de exec: timeout do request > timeout do turno
    assert driver.requests[0].timeout_seconds > payload["timeout_seconds"]


def test_gateway_url_override_for_in_network_reachability(tmp_path, monkeypatch):
    monkeypatch.setenv("DSE_SANDBOX_GATEWAY_URL", "http://model-gateway:4000")
    driver = StubDriver()
    sub = RemoteSubstrate(driver=driver, substrate_name="fake")
    _session(sub, workspace=str(tmp_path))
    sub.run_turn("x")
    assert driver.requests[0].input_payload["gateway"]["base_url"] == "http://model-gateway:4000"


def test_cost_accumulates_and_collect_artifacts(tmp_path):
    driver = StubDriver(
        result=AgentTurnResult(done=True, cost_usd=0.5, tokens_in=100, tokens_out=40).model_dump()
    )
    sub = RemoteSubstrate(driver=driver, substrate_name="claude-agent")
    _session(sub, workspace=str(tmp_path))
    log1 = sub.run_turn("a")
    sub.run_turn("b")
    assert log1.done
    artifacts = sub.collect_artifacts()
    assert artifacts.cost_usd == pytest.approx(1.0)
    assert artifacts.tokens_in == 200 and artifacts.tokens_out == 80
    assert "RemoteSubstrate" in artifacts.diff_summary


def test_runner_failure_raises_typed_error_no_substring(tmp_path):
    driver = StubDriver(
        result=AgentTurnResult(
            done=False, error="turno excedeu 900s", error_kind="timeout"
        ).model_dump()
    )
    sub = RemoteSubstrate(driver=driver, substrate_name="claude-agent")
    _session(sub, workspace=str(tmp_path))
    with pytest.raises(RemoteTurnError) as exc:
        sub.run_turn("x")
    assert exc.value.kind == "timeout"


def test_build_substrate_mode_is_deployment_config(monkeypatch):
    monkeypatch.delenv("DSE_SANDBOX_INPROCESS", raising=False)
    assert isinstance(_build_substrate([{"done": True}]), FakeSubstrate)

    monkeypatch.setenv("DSE_SANDBOX_INPROCESS", "0")
    remote = _build_substrate([{"done": True}], stage="coder")
    assert isinstance(remote, RemoteSubstrate)


def test_coder_turn_pipeline_with_remote_substrate(work_item_id, state_dir):
    """E2E sem docker exec: o turno roda 'no sandbox' (StubDriver simula o
    bind mount) e TODO o pós-turno determinístico (prune, revert de testes,
    commit/push escopado, files_changed) permanece no worker, intacto."""
    tenant_id = "tenant-a"
    asyncio.run(provision_sandbox(ProvisionSandboxInput(work_item_id=work_item_id, tenant_id=tenant_id)))
    workspace_dir, _bare = _paths_for(work_item_id)

    driver = StubDriver(
        result=AgentTurnResult(done=True, cost_usd=0.01, tokens_in=10, tokens_out=5).model_dump(),
        write_into=workspace_dir,
        write_files={"src/from_sandbox.py": "VALUE = 42\n"},
    )
    remote = RemoteSubstrate(driver=driver, substrate_name="fake")

    result = asyncio.run(
        _run_coder_turn_impl(
            RunCoderTurnInput(work_item_id=work_item_id, tenant_id=tenant_id, instruction="implemente"),
            substrate=remote,
        )
    )

    assert "src/from_sandbox.py" in result.files_changed
    assert result.cost_usd == pytest.approx(0.01)
    # o request que atravessou a fronteira não carregou o path do host
    assert driver.requests[0].input_payload["workspace_dir"] == "/workspace"

    asyncio.run(teardown_sandbox(__import__("dse_contracts").TeardownSandboxInput(
        work_item_id=work_item_id, tenant_id=tenant_id
    )))
