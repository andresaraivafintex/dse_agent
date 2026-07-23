"""Plano 08 §G — driver de sandbox Kubernetes (runtime isolado real).

Hoje o agente roda in-process no orchestrator (com a master key + creds no env)
→ o modelo de ameaça "nada para roubar no sandbox" NÃO vale. Este driver executa
cada estágio num Pod efêmero, endurecido e (idealmente) sob um RuntimeClass de
isolamento forte (gVisor/Kata).

O que é CÓDIGO (entregue aqui, testável sem cluster):
  - `build_pod_manifest`: o Pod spec 100% endurecido (o núcleo — a suíte de
    conformidade valida cada propriedade de segurança sem precisar de cluster).
  - `KubernetesSandboxDriver`: implementa o mesmo contrato `SandboxDriver`.

O que precisa de INFRA (prova viva — decisão do cluster do usuário):
  - um cluster com o RuntimeClass (gvisor/kata) instalado;
  - a NetworkPolicy default-deny + egress só para o egress-proxy (documentada);
  - a imagem `agent-runner` publicada no registry do cluster.
Sem cluster/kubectl, `provision`/`execute_stage` FALHAM LIMPO (fail-closed):
NUNCA degradam para execução local (a mesma disciplina do DockerSandboxDriver).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from dse_contracts import (
    CheckpointOpRequest,
    CheckpointOpResult,
    CheckpointRef,
    WorkspaceBootstrapRequest,
    WorkspaceBootstrapResult,
)

from . import docker_driver
from .driver import (
    IsolatedStageExecutionUnavailable,
    SandboxCheckpointRequest,
    SandboxProvisionRequest,
    SandboxRebuildRequest,
    SandboxRebuildResult,
    StageExecutionRequest,
    StageExecutionResult,
)

NONROOT_UID = 10001


@dataclass
class K8sSandboxConfig:
    namespace: str = os.environ.get("DSE_SANDBOX_K8S_NAMESPACE", "dse-sandboxes")
    image: str = os.environ.get("DSE_AGENT_RUNNER_IMAGE", "dse/agent-runner:local")
    # RuntimeClass de isolamento forte. VAZIO = runtime default (isolamento
    # FRACO) — o build_pod_manifest loga/marca isso; produção deve setar.
    runtime_class: str = os.environ.get("DSE_SANDBOX_RUNTIME_CLASS", "gvisor")
    service_account: str = os.environ.get("DSE_SANDBOX_SERVICE_ACCOUNT", "dse-sandbox-runner")
    egress_proxy_url: str = os.environ.get("DSE_EGRESS_PROXY_URL", "http://egress-proxy.dse.svc:3128")
    cpu_limit: str = os.environ.get("DSE_SANDBOX_CPU_LIMIT", "1")
    mem_limit: str = os.environ.get("DSE_SANDBOX_MEM_LIMIT", "2Gi")
    kubectl: str = os.environ.get("DSE_KUBECTL", "kubectl")
    kube_context: str = os.environ.get("DSE_SANDBOX_KUBE_CONTEXT", "")
    # PVC do checkpoint git (/checkpoint.git). Vazio = emptyDir (efêmero —
    # rebuild pós-morte do Pod recomeça do zero); produção/VPS deve apontar
    # um PVC para o rebuild do chaos recuperar o último checkpoint.
    checkpoint_pvc: str = os.environ.get("DSE_SANDBOX_CHECKPOINT_PVC", "")


def pod_name_for(work_item_id: str) -> str:
    slug = "".join(c if c.isalnum() or c == "-" else "-" for c in work_item_id.lower())
    return f"dse-sbx-{slug}"[:63].rstrip("-")


def build_pod_manifest(request: SandboxProvisionRequest, cfg: K8sSandboxConfig | None = None) -> dict[str, Any]:
    """Pod spec efêmero e ENDURECIDO. Núcleo de segurança do §G (testável).

    Endurecimento (cada item é asserido pela suíte de conformidade):
      - runAsNonRoot + UID não-zero (pod e container);
      - allowPrivilegeEscalation=false, privileged=false, cap drop ALL;
      - readOnlyRootFilesystem=true (workspace/tmp em emptyDir graváveis);
      - seccompProfile=RuntimeDefault;
      - automountServiceAccountToken=false;
      - SEM hostPath/socket do Docker, SEM hostNetwork/PID/IPC;
      - egress só via proxy (HTTP(S)_PROXY) — a NetworkPolicy default-deny é
        cluster-side (documentada);
      - restartPolicy=Never (efêmero); limites de CPU/mem."""
    cfg = cfg or K8sSandboxConfig()
    name = pod_name_for(request.work_item_id)
    container_sec = {
        "runAsNonRoot": True,
        "runAsUser": NONROOT_UID,
        "runAsGroup": NONROOT_UID,
        "allowPrivilegeEscalation": False,
        "privileged": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    spec: dict[str, Any] = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "serviceAccountName": cfg.service_account,
        "hostNetwork": False,
        "hostPID": False,
        "hostIPC": False,
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": NONROOT_UID,
            "runAsGroup": NONROOT_UID,
            "fsGroup": NONROOT_UID,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [
            {
                "name": "agent-runner",
                "image": cfg.image,
                "imagePullPolicy": "IfNotPresent",
                "securityContext": container_sec,
                "resources": {
                    "limits": {"cpu": cfg.cpu_limit, "memory": cfg.mem_limit},
                    "requests": {"cpu": "250m", "memory": "512Mi"},
                },
                "env": [
                    {"name": "HTTP_PROXY", "value": cfg.egress_proxy_url},
                    {"name": "HTTPS_PROXY", "value": cfg.egress_proxy_url},
                    {"name": "NO_PROXY", "value": "localhost,127.0.0.1,.svc,.cluster.local"},
                    {"name": "DSE_WORK_ITEM_ID", "value": request.work_item_id},
                    {"name": "DSE_TENANT_ID", "value": request.tenant_id},
                    {"name": "DSE_TASK_BRANCH", "value": request.branch},
                ],
                "volumeMounts": [
                    {"name": "workspace", "mountPath": "/workspace"},
                    {"name": "checkpoint", "mountPath": "/checkpoint.git"},
                    {"name": "tmp", "mountPath": "/tmp"},
                ],
            }
        ],
        "volumes": [
            {"name": "workspace", "emptyDir": {}},
            (
                {"name": "checkpoint", "persistentVolumeClaim": {"claimName": cfg.checkpoint_pvc}}
                if cfg.checkpoint_pvc
                else {"name": "checkpoint", "emptyDir": {}}
            ),
            {"name": "tmp", "emptyDir": {}},
        ],
    }
    # RuntimeClass de isolamento forte: setado só quando configurado. Vazio =
    # runtime default (isolamento fraco) — marcamos no annotation p/ o operador.
    annotations = {}
    if cfg.runtime_class:
        spec["runtimeClassName"] = cfg.runtime_class
    else:
        annotations["dse.fintex/isolation-warning"] = "no RuntimeClass — weak isolation"
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": cfg.namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "dse-sandbox",
                "dse.fintex/work-item": request.work_item_id,
                "dse.fintex/tenant": request.tenant_id,
            },
            "annotations": annotations,
        },
        "spec": spec,
    }


class KubernetesSandboxDriver:
    """Driver K8s: mesmo contrato do DockerSandboxDriver, mas com execução
    isolada real. Fail-closed sem cluster/kubectl (nunca roda local)."""

    def __init__(self, cfg: K8sSandboxConfig | None = None) -> None:
        self._cfg = cfg or K8sSandboxConfig()

    @property
    def supports_isolated_stage_execution(self) -> bool:
        return True

    def sandbox_id_for(self, work_item_id: str) -> str:
        return pod_name_for(work_item_id)

    def _kubectl(self, args: list[str], *, input_text: str | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
        if shutil.which(self._cfg.kubectl) is None:
            raise IsolatedStageExecutionUnavailable(
                f"kubectl ({self._cfg.kubectl}) não encontrado — runtime K8s indisponível; "
                "execução local é proibida como fallback (§G fail-closed)"
            )
        ctx = ["--context", self._cfg.kube_context] if self._cfg.kube_context else []
        proc = subprocess.run(
            [self._cfg.kubectl, *ctx, *args],
            input=input_text, capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0:
            raise IsolatedStageExecutionUnavailable(
                f"kubectl {' '.join(args)} falhou (exit={proc.returncode}): {proc.stderr.strip()}"
            )
        return proc

    def _exec_op(self, pod_name: str, op: str, payload: dict[str, Any], *, timeout: int = 180) -> dict[str, Any]:
        """Roda uma op de lifecycle do runner DENTRO do Pod (`--op bootstrap|
        checkpoint`) — o driver K8s nunca opera git em path do host."""
        proc = self._kubectl(
            ["exec", "-i", pod_name, "-n", self._cfg.namespace, "--",
             "python", "-m", "agent_runner", "--op", op],
            input_text=json.dumps({"input": payload}),
            timeout=timeout,
        )
        try:
            return json.loads((proc.stdout or "").strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as exc:
            raise IsolatedStageExecutionUnavailable(
                f"agent-runner --op {op} devolveu stdout não-JSON: {(proc.stdout or '')[:200]!r}"
            ) from exc

    def _bootstrap(self, request: SandboxProvisionRequest) -> WorkspaceBootstrapResult:
        name = pod_name_for(request.work_item_id)
        out = self._exec_op(
            name, "bootstrap",
            WorkspaceBootstrapRequest(
                work_item_id=request.work_item_id, branch=request.branch,
            ).model_dump(),
        )
        result = WorkspaceBootstrapResult.model_validate(out)
        if result.failed:
            raise IsolatedStageExecutionUnavailable(
                f"bootstrap do workspace falhou no Pod {name}: [{result.error_kind}] {result.error}"
            )
        return result

    def provision(self, request: SandboxProvisionRequest) -> docker_driver.ProvisionedSandbox:
        manifest = build_pod_manifest(request, self._cfg)
        self._kubectl(["apply", "-f", "-"], input_text=json.dumps(manifest))
        name = pod_name_for(request.work_item_id)
        self._kubectl(["wait", "--for=condition=Ready", f"pod/{name}", "-n", self._cfg.namespace, "--timeout=120s"])
        self._bootstrap(request)
        return docker_driver.ProvisionedSandbox(
            container_id=name,
            container_name=name,
            work_item_id=request.work_item_id,
            tenant_id=request.tenant_id,
            branch=request.branch,
            workspace_host_path=request.workspace_path,
            checkpoint_bare_repo_path=request.checkpoint_path,
            resource_caps=docker_driver.ResourceCaps.from_budget(request.budget),
            created_new=True,
        )

    def execute_stage(self, request: StageExecutionRequest) -> StageExecutionResult:
        started = time.time()
        payload = json.dumps({"stage": request.stage.value, "input": request.input_payload})
        proc = self._kubectl(
            ["exec", "-i", request.sandbox_id, "-n", self._cfg.namespace, "--",
             "python", "-m", "agent_runner", "--stage", request.stage.value],
            input_text=payload,
            timeout=int(request.timeout_seconds),
        )
        try:
            out = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            out = {"raw": proc.stdout}
        return StageExecutionResult(
            stage=request.stage,
            output_payload=out,
            exit_code=0,
            duration_seconds=time.time() - started,
        )

    def checkpoint(self, request: SandboxCheckpointRequest) -> CheckpointRef:
        # No K8s o workspace vive num volume do Pod — o commit/push acontece
        # DENTRO dele, contra /checkpoint.git (PVC/emptyDir), com o mesmo
        # refspec fixo + hook pre-receive do fluxo local.
        pod = pod_name_for(request.work_item_id)
        out = self._exec_op(
            pod, "checkpoint",
            CheckpointOpRequest(
                work_item_id=request.work_item_id,
                branch=request.branch,
                phase=request.phase,
            ).model_dump(),
        )
        result = CheckpointOpResult.model_validate(out)
        if result.failed:
            raise IsolatedStageExecutionUnavailable(
                f"checkpoint falhou no Pod {pod}: [{result.error_kind}] {result.error}"
            )
        return CheckpointRef(
            work_item_id=request.work_item_id, git_ref=result.sha, phase=result.phase
        )

    def rebuild(self, request: SandboxRebuildRequest) -> SandboxRebuildResult:
        # Pod novo montando o MESMO volume de checkpoint (PVC): o bootstrap
        # dentro do provision encontra o branch e clona — recuperação do
        # chaos test sem nenhum git no host. Com emptyDir (dev), o checkpoint
        # morre com o Pod e o bootstrap recomeça do zero (sha novo).
        sandbox = self.provision(request.provision)
        state = self._bootstrap(request.provision)
        return SandboxRebuildResult(sandbox=sandbox, recovered_sha=state.sha)

    def teardown(self, sandbox_id: str) -> float:
        started = time.time()
        self._kubectl(["delete", "pod", sandbox_id, "-n", self._cfg.namespace, "--ignore-not-found", "--grace-period=5"])
        return time.time() - started
