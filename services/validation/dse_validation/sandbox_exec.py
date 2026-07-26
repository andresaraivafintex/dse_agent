"""Abstraction for running a command inside the sandbox.

`dse_contracts.activities.SandboxHandle` (owner: WS-C) only carries the handle's
data (sandbox_id, container_id, branch...) — it does not expose a way to run a
command. WS-E needs that for the L1 pipeline (WSE-E1-T1: lint/typecheck/test/
build INSIDE the sandbox). Until `services/sandbox-runtime` (WS-C) publishes its
own execution interface, WS-E defines a minimal Protocol (`SandboxExecutor`)
here plus two implementations:

  - `DockerExecSandbox`  — real: `docker exec <container_id> ...`. Works as soon
    as WS-C provisions the container and populates `SandboxHandle.container_id` —
    it depends on no WS-C code beyond that field, which is already in the
    published contract.
  - `LocalFakeSandbox`   — local/test mode: runs the same command via
    `subprocess` in a local directory (no Docker), so the whole L1 pipeline logic
    can be tested before WS-C ships the real runtime.

If `services/sandbox-runtime` later publishes a richer execution interface (e.g.
streaming, native timeout), swap only the `DockerExecSandbox` implementation —
the `SandboxExecutor` interface and the L1 pipeline that consumes it do not change.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel


class ExecResult(BaseModel):
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


class SandboxExecutor(Protocol):
    def run(self, argv: list[str], cwd: str | None = None, timeout: int = 300) -> ExecResult: ...


class DockerExecSandbox:
    """Runs commands via `docker exec` in the sandbox container (WS-C)."""

    def __init__(self, container_id: str, default_cwd: str = "/workspace/repo"):
        self.container_id = container_id
        self.default_cwd = default_cwd

    def run(self, argv: list[str], cwd: str | None = None, timeout: int = 300) -> ExecResult:
        full_argv = ["docker", "exec", "-w", cwd or self.default_cwd, self.container_id, *argv]
        try:
            proc = subprocess.run(
                full_argv, capture_output=True, text=True, timeout=timeout
            )
            return ExecResult(
                argv=argv, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr
            )
        except subprocess.TimeoutExpired as e:
            return ExecResult(
                argv=argv,
                returncode=-1,
                stdout=(e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or ""),
                stderr=(e.stderr or b"").decode() if isinstance(e.stderr, bytes) else (e.stderr or ""),
                timed_out=True,
            )


class KubectlExecSandbox:
    """Runs commands via ``kubectl exec`` in the sandbox Pod (K8s driver).

    Mirrors ``DockerExecSandbox`` for the K8s runtime: under the K8s driver,
    ``SandboxHandle.container_id`` is the Pod NAME (see ``activities.py``,
    ``container_id = driver.sandbox_id_for``). Runs from the orchestrator worker,
    which has ``kubectl`` and ``pods/exec`` RBAC in the sandbox namespace
    (least-privilege, verified). ``kubectl exec`` has no working-dir flag, so we
    wrap the command in ``sh -c 'cd <cwd> && exec <argv>'`` with every argument
    properly quoted.
    """

    def __init__(
        self,
        pod_name: str,
        namespace: str | None = None,
        default_cwd: str = "/workspace",
        kubectl: str | None = None,
        context: str | None = None,
    ):
        self.pod_name = pod_name
        self.namespace = namespace or os.environ.get("DSE_SANDBOX_K8S_NAMESPACE", "dse-sandboxes")
        self.default_cwd = default_cwd
        self.kubectl = kubectl or os.environ.get("DSE_KUBECTL", "kubectl")
        self.context = context if context is not None else os.environ.get("DSE_SANDBOX_KUBE_CONTEXT", "")

    def run(self, argv: list[str], cwd: str | None = None, timeout: int = 300) -> ExecResult:
        import shlex

        workdir = cwd or self.default_cwd
        inner = "cd " + shlex.quote(workdir) + " && exec " + " ".join(shlex.quote(a) for a in argv)
        base = [self.kubectl]
        if self.context:
            base += ["--context", self.context]
        full_argv = base + ["exec", "-i", self.pod_name, "-n", self.namespace, "--", "sh", "-c", inner]
        try:
            proc = subprocess.run(full_argv, capture_output=True, text=True, timeout=timeout)
            return ExecResult(
                argv=argv, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr
            )
        except subprocess.TimeoutExpired as e:
            return ExecResult(
                argv=argv,
                returncode=-1,
                stdout=(e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or ""),
                stderr=(e.stderr or b"").decode() if isinstance(e.stderr, bytes) else (e.stderr or ""),
                timed_out=True,
            )


class LocalFakeSandbox:
    """Local/test mode: runs the command directly in a local directory, without
    Docker. Used by the `dse_validation` tests to prove the L1 pipeline logic
    (finding parsing, diff-budget, forbidden-paths) against REAL bandit/ruff/
    pytest/git executions (it does not mock the tool, only the container
    isolation that WS-C has not published yet)."""

    def __init__(self, repo_dir: str | Path):
        self.repo_dir = Path(repo_dir)

    def run(self, argv: list[str], cwd: str | None = None, timeout: int = 300) -> ExecResult:
        workdir = Path(cwd) if cwd else self.repo_dir
        try:
            proc = subprocess.run(
                argv, cwd=str(workdir), capture_output=True, text=True, timeout=timeout
            )
            return ExecResult(
                argv=argv, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr
            )
        except FileNotFoundError as e:
            return ExecResult(argv=argv, returncode=127, stdout="", stderr=str(e))
        except subprocess.TimeoutExpired as e:
            return ExecResult(
                argv=argv,
                returncode=-1,
                stdout=(e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or ""),
                stderr=(e.stderr or b"").decode() if isinstance(e.stderr, bytes) else (e.stderr or ""),
                timed_out=True,
            )


def executor_for_handle(sandbox_handle, repo_dir: str = "/workspace/repo") -> SandboxExecutor:
    """Resolves the real executor from a `SandboxHandle` (WS-C). Used by the
    Activity wrapper (`activities.py`) — the tests call the core logic directly
    with an injected `LocalFakeSandbox`, without going through here.
    """
    # S7 (Phase 5) — the PoC's IN-PROCESS mode. When the agent substrate runs in
    # the control-plane process (claude-agent-sdk in the orchestrator, not INSIDE
    # the ephemeral container), the real code lives in the LOCAL workspace
    # (`$DSE_SANDBOX_STATE_DIR/<wi>/workspace`) — clone, Coder and checkpoint all
    # run there. The container's `/workspace` is EMPTY (docker-out-of-docker: the
    # daemon mounts a host path that does not match the orchestrator's internal
    # path). So L1 and finalize (git push) MUST operate on that local workspace,
    # via subprocess. PRODUCTION (agent INSIDE the container) keeps DockerExec —
    # this branch is env-gated and OFF by default, so it changes neither
    # production behavior nor the WS-C/WS-E tests.
    deployment_profile = os.environ.get("DSE_DEPLOYMENT_PROFILE", "dev").strip().lower()
    inprocess = os.environ.get("DSE_SANDBOX_INPROCESS", "0").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    if deployment_profile in {"pilot", "prod", "production"} and inprocess:
        raise RuntimeError(
            "production profile refuses DSE_SANDBOX_INPROCESS: L1 must run "
            "in the isolated sandbox"
        )
    if inprocess:
        wi = getattr(sandbox_handle, "work_item_id", None)
        if not wi:
            raise RuntimeError("DSE_SANDBOX_INPROCESS=1 requires a SandboxHandle with work_item_id")
        state_dir = os.environ.get("DSE_SANDBOX_STATE_DIR", "/tmp/dse-sandboxes")
        local_ws = Path(state_dir) / wi / "workspace"
        return LocalFakeSandbox(local_ws)
    # K8s driver (PoC/isolated production): the agent runs INSIDE the sandbox Pod;
    # the handle's "container_id" is the Pod NAME. L1/finalize must operate on
    # that Pod via `kubectl exec` — not `docker exec` (there is no Docker on the
    # node). The K8s runtime's default_cwd is /workspace (where the runner clones),
    # not /workspace/repo (docker layout). Gated by the driver's own env var.
    driver = os.environ.get("DSE_SANDBOX_DRIVER", "").strip().lower()
    if driver in {"k8s", "kubernetes"} and getattr(sandbox_handle, "container_id", None):
        return KubectlExecSandbox(sandbox_handle.container_id)
    if getattr(sandbox_handle, "container_id", None):
        return DockerExecSandbox(sandbox_handle.container_id, default_cwd=repo_dir)
    raise RuntimeError(
        "SandboxHandle without container_id — WS-C has not provisioned the real sandbox yet; "
        "use LocalFakeSandbox explicitly in tests/dev."
    )
