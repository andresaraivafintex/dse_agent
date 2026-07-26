"""Rootless Docker driver for the per-task ephemeral sandbox (WSC-E1-T1/T2).

Isolation guarantees applied to EVERY sandbox container created by this module:
  - non-root `--user <uid>:<gid>` (never root, never uid 0).
  - No mount of `/var/run/docker.sock` (nor any Docker socket) — it never
    appears in `HostConfig.Binds`/`Mounts`.
  - `--read-only` (the container's root filesystem is read-only) +
    `--tmpfs /tmp` (an ephemeral writable layer for scratch only).
  - `--cap-drop ALL` + `--security-opt no-new-privileges` (no escalation).
  - Network: attached exclusively to the internal `dse_sandbox_net` network
    (`internal=True` — no internet gateway). The only host reachable from
    inside the container is the egress-proxy, which is itself also attached to
    a network with an internet route (`dse_net`).
  - `--cpus`, `--memory`, `--pids-limit` derived from the WorkItem's `budget`.
  - A `dse.work_item_id=<id>` label on every container — used both for
    provisioning idempotency and for discovery at teardown/kill.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

import docker
from docker.errors import NotFound
from docker.models.containers import Container

SANDBOX_NETWORK_NAME = "dse_sandbox_net"
LABEL_WORK_ITEM = "dse.work_item_id"
LABEL_TENANT = "dse.tenant_id"
LABEL_COMPONENT = "dse.component"
LABEL_ROLE = "dse.role"  # "sandbox" | "egress_proxy" | "checkpoint_helper"

# S7-c (Phase 5): the sandbox image must carry the tools that run INSIDE it via
# `docker exec` — git (finalize_pr push) and the repo's toolchain (e.g. node for
# L1's `node --check` on a JS repo). `dse-sandbox-base` (WSC-E3-T4b) ships
# git+node+Playwright. Configurable by env so it is not pinned here.
DEFAULT_SANDBOX_IMAGE = os.environ.get("DSE_SANDBOX_IMAGE", "python:3.11-slim")
DEFAULT_NONROOT_USER = "10001:10001"

# Resource class -> defaults, used when WorkItem.budget does not specify an
# explicit value. Keeps parity with the "resource class" vocabulary used in the
# OTel metrics (dse.resource_class).
_RESOURCE_CLASS_DEFAULTS: dict[str, dict[str, Any]] = {
    "small": {"cpu_limit": 0.5, "memory_mb": 256, "pids_limit": 128},
    "medium": {"cpu_limit": 1.0, "memory_mb": 512, "pids_limit": 256},
    "large": {"cpu_limit": 2.0, "memory_mb": 1024, "pids_limit": 512},
}


@dataclass
class ResourceCaps:
    resource_class: str
    cpu_limit: float
    memory_mb: int
    pids_limit: int
    # Phase 3 (WSC-E3-T4b): size of the /tmp tmpfs. The Phase 1 default of 64MB
    # stays; the @demo evidence run (headless chromium writes scratch/crashpad
    # to /tmp) uses `budget={"tmp_mb": 256}`. Additive — it changes no existing
    # behavior.
    tmp_mb: int = 64

    @classmethod
    def from_budget(cls, budget: dict[str, Any] | None) -> "ResourceCaps":
        budget = budget or {}
        resource_class = str(budget.get("resource_class", "small"))
        defaults = _RESOURCE_CLASS_DEFAULTS.get(resource_class, _RESOURCE_CLASS_DEFAULTS["small"])
        return cls(
            resource_class=resource_class,
            cpu_limit=float(budget.get("cpu_limit", defaults["cpu_limit"])),
            memory_mb=int(budget.get("memory_mb", defaults["memory_mb"])),
            pids_limit=int(budget.get("pids_limit", defaults["pids_limit"])),
            tmp_mb=int(budget.get("tmp_mb", 64)),
        )


@dataclass
class ProvisionedSandbox:
    container_id: str
    container_name: str
    work_item_id: str
    tenant_id: str
    branch: str
    workspace_host_path: str
    checkpoint_bare_repo_path: str
    resource_caps: ResourceCaps
    created_new: bool  # False when an existing container was idempotently reused
    started_at: float = field(default_factory=time.time)


def _client() -> docker.DockerClient:
    return docker.from_env()


def ensure_sandbox_network(client: docker.DockerClient | None = None) -> str:
    """Create (if needed) the internal Docker network with no internet route that
    every sandbox uses. Idempotent."""
    client = client or _client()
    try:
        client.networks.get(SANDBOX_NETWORK_NAME)
    except NotFound:
        client.networks.create(
            SANDBOX_NETWORK_NAME,
            driver="bridge",
            internal=True,  # no internet gateway — the core isolation property
            check_duplicate=True,
            labels={LABEL_COMPONENT: "sandbox-runtime"},
        )
    return SANDBOX_NETWORK_NAME


def _container_name(work_item_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in work_item_id)
    return f"dse-sandbox-{safe}"


def container_name_for(work_item_id: str) -> str:
    """Public, stable name of this work item's sandbox container — used by
    `SandboxDriver.sandbox_id_for` (the target of the agent-runner's
    `docker exec`). Same derivation as at creation time (idempotency by
    name+label)."""
    return _container_name(work_item_id)


def find_existing_container(work_item_id: str, client: docker.DockerClient | None = None) -> Container | None:
    """Provisioning idempotency (WSC-E1-T3): look up by label before creating.
    Considers any state (running/exited) — it reuses."""
    client = client or _client()
    # Filter by work_item_id on the daemon and by role on the client (docker-py
    # does not portably guarantee an AND across multiple values of the same
    # 'label' key across daemon versions).
    containers = [
        c
        for c in client.containers.list(all=True, filters={"label": f"{LABEL_WORK_ITEM}={work_item_id}"})
        if c.labels.get(LABEL_ROLE) == "sandbox"
    ]
    return containers[0] if containers else None


def provision_container(
    *,
    work_item_id: str,
    tenant_id: str,
    branch: str,
    workspace_host_path: str,
    checkpoint_bare_repo_path: str,
    budget: dict[str, Any] | None = None,
    image: str = DEFAULT_SANDBOX_IMAGE,
    user: str = DEFAULT_NONROOT_USER,
    client: docker.DockerClient | None = None,
) -> ProvisionedSandbox:
    client = client or _client()
    caps = ResourceCaps.from_budget(budget)

    existing = find_existing_container(work_item_id, client=client)
    if existing is not None:
        existing.reload()
        return ProvisionedSandbox(
            container_id=existing.id,
            container_name=existing.name,
            work_item_id=work_item_id,
            tenant_id=tenant_id,
            branch=branch,
            workspace_host_path=workspace_host_path,
            checkpoint_bare_repo_path=checkpoint_bare_repo_path,
            resource_caps=caps,
            created_new=False,
        )

    ensure_sandbox_network(client)
    name = _container_name(work_item_id)

    container = client.containers.run(
        image,
        name=name,
        command=["sleep", "infinity"],
        detach=True,
        user=user,
        read_only=True,
        tmpfs={"/tmp": f"size={caps.tmp_mb}m,mode=1777"},
        cap_drop=["ALL"],
        security_opt=["no-new-privileges"],
        network=SANDBOX_NETWORK_NAME,
        mem_limit=f"{caps.memory_mb}m",
        nano_cpus=int(caps.cpu_limit * 1_000_000_000),
        pids_limit=caps.pids_limit,
        volumes={
            workspace_host_path: {"bind": "/workspace", "mode": "rw"},
            checkpoint_bare_repo_path: {"bind": "/checkpoint.git", "mode": "rw"},
        },
        working_dir="/workspace",
        labels={
            LABEL_WORK_ITEM: work_item_id,
            LABEL_TENANT: tenant_id,
            LABEL_COMPONENT: "sandbox-runtime",
            LABEL_ROLE: "sandbox",
            "dse.resource_class": caps.resource_class,
        },
        # No docker.sock bind, no --privileged, no extra_hosts pointing at the
        # host — the only way out of the internal network is the egress-proxy,
        # which is also attached to `dse_sandbox_net`.
    )

    return ProvisionedSandbox(
        container_id=container.id,
        container_name=container.name,
        work_item_id=work_item_id,
        tenant_id=tenant_id,
        branch=branch,
        workspace_host_path=workspace_host_path,
        checkpoint_bare_repo_path=checkpoint_bare_repo_path,
        resource_caps=caps,
        created_new=True,
    )


def kill_container(container_id: str, client: docker.DockerClient | None = None, signal: str = "SIGKILL") -> None:
    """Used by the chaos test (WSC-E1-T4): kills the container mid-task, without
    going through a graceful teardown."""
    client = client or _client()
    try:
        c = client.containers.get(container_id)
        c.kill(signal=signal)
    except NotFound:
        pass


def teardown_container(container_id: str, client: docker.DockerClient | None = None) -> float:
    """Remove the container and return its runtime in minutes (for the OTel metric)."""
    client = client or _client()
    try:
        c = client.containers.get(container_id)
        c.reload()
        started_at_str = c.attrs["State"].get("StartedAt")
        finished_at = time.time()
        runtime_minutes = 0.0
        if started_at_str and started_at_str != "0001-01-01T00:00:00Z":
            import datetime as _dt

            started_at = _dt.datetime.fromisoformat(started_at_str.replace("Z", "+00:00"))
            runtime_minutes = max(
                0.0, (finished_at - started_at.timestamp()) / 60.0
            )
        c.remove(force=True)
        return runtime_minutes
    except NotFound:
        return 0.0


def inspect_no_docker_sock(container_id: str, client: docker.DockerClient | None = None) -> bool:
    """True if NO container mount references the host's Docker socket — used by
    the isolation test (WSC-E1-T1)."""
    client = client or _client()
    c = client.containers.get(container_id)
    c.reload()
    for m in c.attrs.get("Mounts", []):
        src = m.get("Source", "")
        if "docker.sock" in src:
            return False
    return True


def inspect_non_root_user(container_id: str, client: docker.DockerClient | None = None) -> str:
    client = client or _client()
    c = client.containers.get(container_id)
    c.reload()
    return c.attrs["Config"].get("User", "")
