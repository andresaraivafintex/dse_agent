"""plan 08 §G — Kubernetes sandbox driver (real isolated runtime).

Today the agent runs in-process in the orchestrator (with the master key + creds
in the env) → the "nothing to steal inside the sandbox" threat model does NOT
hold. This driver executes each stage in an ephemeral, hardened Pod, ideally
under a strong-isolation RuntimeClass (gVisor/Kata).

What is CODE (delivered here, testable without a cluster):
  - `build_pod_manifest`: the fully hardened Pod spec (the core — the
    conformance suite validates every security property with no cluster).
  - `KubernetesSandboxDriver`: implements the same `SandboxDriver` contract.

What needs INFRA (live proof — the user's cluster decision):
  - a cluster with the RuntimeClass (gvisor/kata) installed;
  - the default-deny NetworkPolicy + egress only to the egress-proxy
    (documented);
  - the `agent-runner` image published to the cluster's registry.
Without a cluster/kubectl, `provision`/`execute_stage` FAIL CLEANLY
(fail-closed): they NEVER degrade to local execution (the same discipline as
DockerSandboxDriver).
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
    # Strong-isolation RuntimeClass. EMPTY = default runtime (WEAK isolation) —
    # build_pod_manifest logs/flags that; production must set it.
    runtime_class: str = os.environ.get("DSE_SANDBOX_RUNTIME_CLASS", "gvisor")
    service_account: str = os.environ.get("DSE_SANDBOX_SERVICE_ACCOUNT", "dse-sandbox-runner")
    # Default FQDN + port 8806 (the real value comes from the configmap via env;
    # this default only applies outside the chart and avoids the stale port 3128
    # footgun).
    egress_proxy_url: str = os.environ.get("DSE_EGRESS_PROXY_URL", "http://egress-proxy.dse.svc.cluster.local:8806")
    cpu_limit: str = os.environ.get("DSE_SANDBOX_CPU_LIMIT", "1")
    mem_limit: str = os.environ.get("DSE_SANDBOX_MEM_LIMIT", "2Gi")
    kubectl: str = os.environ.get("DSE_KUBECTL", "kubectl")
    kube_context: str = os.environ.get("DSE_SANDBOX_KUBE_CONTEXT", "")
    # PVC for the git checkpoint (/checkpoint.git). Empty = emptyDir (ephemeral —
    # a rebuild after the Pod dies starts from scratch); production/VPS must
    # point at a PVC so the chaos rebuild can recover the last checkpoint.
    checkpoint_pvc: str = os.environ.get("DSE_SANDBOX_CHECKPOINT_PVC", "")


def pod_name_for(work_item_id: str) -> str:
    slug = "".join(c if c.isalnum() or c == "-" else "-" for c in work_item_id.lower())
    return f"dse-sbx-{slug}"[:63].rstrip("-")


def _label_value(v: str) -> str:
    """K8s label value: at most 63 chars, not ending in -/_/.

    The real work_item_id is `wi_` + sha256 (64 hex) = 67 chars, which blows the
    limit and makes the Pod's `kubectl apply` fail (invalid metadata.labels). We
    truncate while preserving the recognizable prefix. This label is
    INFORMATIONAL — no selector uses it (Pods are addressed via pod_name_for)."""
    return v[:63].rstrip("-_.")


def build_pod_manifest(request: SandboxProvisionRequest, cfg: K8sSandboxConfig | None = None) -> dict[str, Any]:
    """Ephemeral, HARDENED Pod spec. The security core of §G (testable).

    Hardening (every item is asserted by the conformance suite):
      - runAsNonRoot + non-zero UID (pod and container);
      - allowPrivilegeEscalation=false, privileged=false, cap drop ALL;
      - readOnlyRootFilesystem=true (workspace/tmp are writable emptyDirs);
      - seccompProfile=RuntimeDefault;
      - automountServiceAccountToken=false;
      - NO hostPath/Docker socket, NO hostNetwork/PID/IPC;
      - egress only through the proxy (HTTP(S)_PROXY) — the default-deny
        NetworkPolicy is cluster-side (documented);
      - restartPolicy=Never (ephemeral); CPU/mem limits."""
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
    # Strong-isolation RuntimeClass: set only when configured. Empty = default
    # runtime (weak isolation) — we flag it in an annotation for the operator.
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
                "dse.fintex/work-item": _label_value(request.work_item_id),
                "dse.fintex/tenant": _label_value(request.tenant_id),
            },
            "annotations": annotations,
        },
        "spec": spec,
    }


class KubernetesSandboxDriver:
    """K8s driver: same contract as DockerSandboxDriver, but with real isolated
    execution. Fail-closed without a cluster/kubectl (never runs locally)."""

    def __init__(self, cfg: K8sSandboxConfig | None = None) -> None:
        self._cfg = cfg or K8sSandboxConfig()

    @property
    def supports_isolated_stage_execution(self) -> bool:
        return True

    @property
    def workspace_is_host_visible(self) -> bool:
        return False  # the workspace lives in the Pod volume — git/hygiene via ops

    def sandbox_id_for(self, work_item_id: str) -> str:
        return pod_name_for(work_item_id)

    def execute_op(
        self, sandbox_id: str, op: str, payload: dict[str, Any], *, timeout_seconds: float = 180.0
    ) -> dict[str, Any]:
        return self._exec_op(sandbox_id, op, payload, timeout=int(timeout_seconds))

    def _kubectl(self, args: list[str], *, input_text: str | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
        if shutil.which(self._cfg.kubectl) is None:
            raise IsolatedStageExecutionUnavailable(
                f"kubectl ({self._cfg.kubectl}) not found — K8s runtime unavailable; "
                "local execution is forbidden as a fallback (§G fail-closed)"
            )
        ctx = ["--context", self._cfg.kube_context] if self._cfg.kube_context else []
        proc = subprocess.run(
            [self._cfg.kubectl, *ctx, *args],
            input=input_text, capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0:
            raise IsolatedStageExecutionUnavailable(
                f"kubectl {' '.join(args)} failed (exit={proc.returncode}): {proc.stderr.strip()}"
            )
        return proc

    def _exec_op(self, pod_name: str, op: str, payload: dict[str, Any], *, timeout: int = 180) -> dict[str, Any]:
        """Run a runner lifecycle op INSIDE the Pod (`--op bootstrap|checkpoint`)
        — the K8s driver never operates git on a host path."""
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
                f"agent-runner --op {op} returned non-JSON stdout: {(proc.stdout or '')[:200]!r}"
            ) from exc

    def _bootstrap(self, request: SandboxProvisionRequest) -> WorkspaceBootstrapResult:
        name = pod_name_for(request.work_item_id)
        out = self._exec_op(
            name, "bootstrap",
            WorkspaceBootstrapRequest(
                work_item_id=request.work_item_id, branch=request.branch,
                base_branch=request.base_branch, repo=request.repo,
            ).model_dump(),
        )
        result = WorkspaceBootstrapResult.model_validate(out)
        if result.failed:
            raise IsolatedStageExecutionUnavailable(
                f"workspace bootstrap failed in Pod {name}: [{result.error_kind}] {result.error}"
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
        # On K8s the workspace lives in a Pod volume — the commit/push happens
        # INSIDE it, against /checkpoint.git (PVC/emptyDir), with the same fixed
        # refspec + pre-receive hook as the local flow.
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
                f"checkpoint failed in Pod {pod}: [{result.error_kind}] {result.error}"
            )
        return CheckpointRef(
            work_item_id=request.work_item_id, git_ref=result.sha, phase=result.phase
        )

    def rebuild(self, request: SandboxRebuildRequest) -> SandboxRebuildResult:
        # A fresh Pod mounting the SAME checkpoint volume (PVC): the bootstrap
        # inside provision finds the branch and clones — chaos-test recovery
        # with no git on the host at all. With emptyDir (dev), the checkpoint
        # dies with the Pod and the bootstrap starts from scratch (new sha).
        sandbox = self.provision(request.provision)
        state = self._bootstrap(request.provision)
        return SandboxRebuildResult(sandbox=sandbox, recovered_sha=state.sha)

    def teardown(self, sandbox_id: str) -> float:
        started = time.time()
        self._kubectl(["delete", "pod", sandbox_id, "-n", self._cfg.namespace, "--ignore-not-found", "--grace-period=5"])
        return time.time() - started
