"""Contrato único para runtimes de sandbox.

O adapter Docker existente continua sendo a implementação do lifecycle local
sem alteração nas funções públicas de ``docker_driver``/``git_checkpoint``.
``execute_stage`` entrega o payload tipado ao agent-runner DENTRO do sandbox
(`docker exec -i` aqui; `kubectl exec -i` no driver K8s) e é fail-closed em
qualquer indisponibilidade — este contrato nunca degrada para
``agent.run_turn()`` no worker (invariante 2 da spec).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from dse_contracts import CheckpointRef, Stage

from . import docker_driver, git_checkpoint


@dataclass(frozen=True)
class SandboxProvisionRequest:
    work_item_id: str
    tenant_id: str
    branch: str
    workspace_path: str
    checkpoint_path: str
    budget: dict[str, Any] = field(default_factory=dict)
    image: str = docker_driver.DEFAULT_SANDBOX_IMAGE
    user: str = docker_driver.DEFAULT_NONROOT_USER


@dataclass(frozen=True)
class StageExecutionRequest:
    """Payload interno que o worker enviará ao agent-runner isolado.

    Não contém paths do host nem credenciais privilegiadas. ``input_payload``
    deve obedecer ao contrato específico do estágio antes de chegar ao driver.
    """

    sandbox_id: str
    work_item_id: str
    tenant_id: str
    stage: Stage
    input_payload: dict[str, Any]
    timeout_seconds: float


@dataclass(frozen=True)
class StageExecutionResult:
    stage: Stage
    output_payload: dict[str, Any]
    exit_code: int
    duration_seconds: float


@dataclass(frozen=True)
class SandboxCheckpointRequest:
    work_item_id: str
    workspace_path: str
    branch: str
    phase: str


@dataclass(frozen=True)
class SandboxRebuildRequest:
    provision: SandboxProvisionRequest
    checkpoint_ref: CheckpointRef


@dataclass(frozen=True)
class SandboxRebuildResult:
    sandbox: docker_driver.ProvisionedSandbox
    recovered_sha: str


class IsolatedStageExecutionUnavailable(RuntimeError):
    """O driver não possui agent-runner isolado; nunca executar localmente."""


@runtime_checkable
class SandboxDriver(Protocol):
    """Lifecycle e execução comuns aos drivers Docker/Kubernetes."""

    @property
    def supports_isolated_stage_execution(self) -> bool:
        ...

    def sandbox_id_for(self, work_item_id: str) -> str:
        """Identidade estável do sandbox deste work item no runtime alvo
        (nome do container no Docker, nome do Pod no K8s)."""
        ...

    def provision(self, request: SandboxProvisionRequest) -> docker_driver.ProvisionedSandbox:
        ...

    def execute_stage(self, request: StageExecutionRequest) -> StageExecutionResult:
        ...

    def checkpoint(self, request: SandboxCheckpointRequest) -> CheckpointRef:
        ...

    def rebuild(self, request: SandboxRebuildRequest) -> SandboxRebuildResult:
        ...

    def teardown(self, sandbox_id: str) -> float:
        ...


class DockerSandboxDriver:
    """Adapter compatível sobre o lifecycle Docker atual.

    ``execute_stage`` (Fase 1, plano 09) roda o agent-runner DENTRO do
    container do sandbox via ``docker exec -i`` — simétrico ao ``kubectl
    exec -i`` do driver K8s. Exige a imagem do sandbox com o módulo
    ``agent_runner`` instalado (``make agent-runner-image``); qualquer
    indisponibilidade (docker ausente, container morto, runner ausente na
    imagem) é falha limpa — NUNCA fallback para execução no worker.
    """

    @property
    def supports_isolated_stage_execution(self) -> bool:
        return True

    def sandbox_id_for(self, work_item_id: str) -> str:
        return docker_driver.container_name_for(work_item_id)

    def provision(self, request: SandboxProvisionRequest) -> docker_driver.ProvisionedSandbox:
        return docker_driver.provision_container(
            work_item_id=request.work_item_id,
            tenant_id=request.tenant_id,
            branch=request.branch,
            workspace_host_path=request.workspace_path,
            checkpoint_bare_repo_path=request.checkpoint_path,
            budget=request.budget,
            image=request.image,
            user=request.user,
        )

    def execute_stage(self, request: StageExecutionRequest) -> StageExecutionResult:
        docker_bin = shutil.which("docker")
        if docker_bin is None:
            raise IsolatedStageExecutionUnavailable(
                "docker CLI não encontrado — execução isolada indisponível; "
                "execução local é proibida como fallback (invariante 2)"
            )
        started = time.time()
        payload = json.dumps({"stage": request.stage.value, "input": request.input_payload})
        try:
            proc = subprocess.run(
                [docker_bin, "exec", "-i", request.sandbox_id,
                 "python", "-m", "agent_runner", "--stage", request.stage.value],
                input=payload,
                capture_output=True,
                text=True,
                timeout=request.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise IsolatedStageExecutionUnavailable(
                f"agent-runner excedeu {request.timeout_seconds:.0f}s no exec "
                f"(stage={request.stage.value!r}, sandbox={request.sandbox_id})"
            ) from exc
        except FileNotFoundError as exc:  # docker sumiu entre o which e o exec
            raise IsolatedStageExecutionUnavailable(
                "docker CLI indisponível no exec — sem fallback local"
            ) from exc
        if proc.returncode != 0:
            raise IsolatedStageExecutionUnavailable(
                f"docker exec agent_runner falhou (exit={proc.returncode}, "
                f"sandbox={request.sandbox_id}): {proc.stderr.strip()[:400]} "
                "— sem fallback local"
            )
        try:
            # última linha JSON: o runner escreve exatamente um resultado
            out = json.loads((proc.stdout or "").strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as exc:
            raise IsolatedStageExecutionUnavailable(
                f"agent-runner devolveu stdout não-JSON (sandbox={request.sandbox_id}): "
                f"{(proc.stdout or '')[:200]!r}"
            ) from exc
        return StageExecutionResult(
            stage=request.stage,
            output_payload=out,
            exit_code=proc.returncode,
            duration_seconds=time.time() - started,
        )

    def checkpoint(self, request: SandboxCheckpointRequest) -> CheckpointRef:
        return git_checkpoint.checkpoint(
            request.work_item_id,
            request.workspace_path,
            request.branch,
            request.phase,
        )

    def rebuild(self, request: SandboxRebuildRequest) -> SandboxRebuildResult:
        provision = request.provision
        rebuilt_workspace = provision.workspace_path
        recovered_sha = git_checkpoint.rebuild_from_checkpoint(
            rebuilt_workspace,
            provision.checkpoint_path,
            provision.branch,
            request.checkpoint_ref,
        )
        sandbox = self.provision(provision)
        return SandboxRebuildResult(sandbox=sandbox, recovered_sha=recovered_sha)

    def teardown(self, sandbox_id: str) -> float:
        return docker_driver.teardown_container(sandbox_id)


def select_sandbox_driver() -> SandboxDriver:
    """Plano 08 §G — escolhe o driver pelo perfil (`DSE_SANDBOX_DRIVER`):
    'docker' (default, dev local) ou 'k8s' (piloto — Pod isolado sob
    RuntimeClass). Import lazy do driver K8s p/ evitar ciclo. No perfil piloto,
    combine com `DSE_SANDBOX_INPROCESS=0` (o fail-closed já recusa execução
    local)."""
    import os

    choice = os.environ.get("DSE_SANDBOX_DRIVER", "docker").strip().lower()
    if choice in ("k8s", "kubernetes"):
        from .k8s_driver import KubernetesSandboxDriver
        return KubernetesSandboxDriver()
    return DockerSandboxDriver()


DEFAULT_SANDBOX_DRIVER: SandboxDriver = select_sandbox_driver()

