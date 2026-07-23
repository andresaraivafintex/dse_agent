"""Prova VIVA do turno isolado em dev (Fase 1, plano 09 — F1.6 parcial).

Executa o agent-runner de verdade dentro do container endurecido do sandbox
(`docker exec -i`, imagem `dse/agent-runner:local` — `make agent-runner-image`)
e prova as duas propriedades que a suíte de conformidade só afirma:

  1. O turno executa DENTRO do sandbox e o worker enxerga a edição apenas
     pelo bind mount do workspace — nenhum SDK no processo do worker.
  2. Tentativas de escrever FORA do /workspace falham por isolamento de SO
     (rootfs read-only, usuário não-root), não por disciplina de toolset —
     e a negação volta como resultado estruturado (P6), nunca fallback.

Estes testes SÃO PULADOS quando a imagem não existe localmente (CI de unidade
não a constrói). A prova equivalente sob gVisor/K8s continua pendente de
cluster — gate pilotReadiness.sandboxIsolationVerified permanece false.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest
from dse_contracts import AgentTurnRequest, AgentTurnResult, Stage

from sandbox_runtime.driver import DockerSandboxDriver, SandboxProvisionRequest, StageExecutionRequest

RUNNER_IMAGE = "dse/agent-runner:local"


def _image_available() -> bool:
    if shutil.which("docker") is None:
        return False
    proc = subprocess.run(
        ["docker", "image", "inspect", RUNNER_IMAGE], capture_output=True, text=True
    )
    return proc.returncode == 0


pytestmark = pytest.mark.skipif(
    not _image_available(),
    reason=f"imagem {RUNNER_IMAGE} ausente — rode `make agent-runner-image`",
)


@pytest.fixture()
def live_sandbox(tmp_path, work_item_id):
    driver = DockerSandboxDriver()
    workspace = tmp_path / "workspace"
    checkpoint = tmp_path / "checkpoint.git"
    workspace.mkdir()
    checkpoint.mkdir()
    sandbox = driver.provision(
        SandboxProvisionRequest(
            work_item_id=work_item_id,
            tenant_id="tenant-live",
            branch=f"dse/{work_item_id}",
            workspace_path=str(workspace),
            checkpoint_path=str(checkpoint),
            image=RUNNER_IMAGE,
        )
    )
    try:
        yield driver, sandbox, workspace
    finally:
        driver.teardown(sandbox.container_id)


def _turn_request(work_item_id: str, fake_script: list[dict]) -> dict:
    return AgentTurnRequest(
        work_item_id=work_item_id,
        tenant_id="tenant-live",
        stage="coder",
        substrate="fake",
        instruction="prova viva",
        fake_script=fake_script,
    # gateway aponta para a rede interna; o turno fake não faz chamada de rede
        gateway={"base_url": "http://model-gateway:4000", "virtual_key": "vk-live"},
    ).model_dump()


def test_turn_executes_inside_sandbox_and_edit_arrives_via_bind_mount(live_sandbox, work_item_id):
    driver, sandbox, workspace = live_sandbox
    result = driver.execute_stage(
        StageExecutionRequest(
            sandbox_id=sandbox.container_name,
            work_item_id=work_item_id,
            tenant_id="tenant-live",
            stage=Stage.coder,
            input_payload=_turn_request(
                work_item_id,
                [{"write_files": {"src/inside.py": "WRITTEN_INSIDE = True\n"},
                  "thought": "editado dentro do sandbox", "done": True}],
            ),
            timeout_seconds=120,
        )
    )
    turn = AgentTurnResult.model_validate(result.output_payload)
    assert turn.done and not turn.failed
    # a edição só chega ao worker pelo bind mount — o marker existe no host
    assert (workspace / "src" / "inside.py").read_text() == "WRITTEN_INSIDE = True\n"


def test_escape_attempts_are_denied_by_os_isolation(live_sandbox, work_item_id, tmp_path):
    driver, sandbox, workspace = live_sandbox

    # 1) path absoluto fora do workspace → rootfs read-only nega (EROFS/EACCES)
    result = driver.execute_stage(
        StageExecutionRequest(
            sandbox_id=sandbox.container_name,
            work_item_id=work_item_id,
            tenant_id="tenant-live",
            stage=Stage.coder,
            input_payload=_turn_request(
                work_item_id, [{"write_files": {"/pwned.txt": "escape"}, "done": True}]
            ),
            timeout_seconds=120,
        )
    )
    turn = AgentTurnResult.model_validate(result.output_payload)
    assert turn.failed and turn.error_kind == "substrate_error"

    # 2) traversal ../ para fora do bind mount → o host NUNCA vê o arquivo
    driver.execute_stage(
        StageExecutionRequest(
            sandbox_id=sandbox.container_name,
            work_item_id=work_item_id,
            tenant_id="tenant-live",
            stage=Stage.coder,
            input_payload=_turn_request(
                work_item_id, [{"write_files": {"../escape.txt": "escape"}, "done": True}]
            ),
            timeout_seconds=120,
        )
    )
    assert not (tmp_path / "escape.txt").exists()
    assert not (workspace.parent / "escape.txt").exists()
