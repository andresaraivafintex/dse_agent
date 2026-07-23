"""Driver Docker rootless para o sandbox efêmero por tarefa (WSC-E1-T1/T2).

Garantias de isolamento aplicadas em TODO container de sandbox criado por
este módulo:
  - `--user <uid>:<gid>` não-root (nunca root, nunca uid 0).
  - Sem montagem de `/var/run/docker.sock` (nem qualquer socket Docker) —
    nunca aparece em `HostConfig.Binds`/`Mounts`.
  - `--read-only` (root filesystem do container é somente-leitura) +
    `--tmpfs /tmp` (camada gravável efêmera só para escratch).
  - `--cap-drop ALL` + `--security-opt no-new-privileges` (sem escalada).
  - Rede: conectado exclusivamente à rede interna `dse_sandbox_net`
    (`internal=True` — sem gateway de internet). O único host alcançável de
    dentro do container é o egress-proxy, que por sua vez está conectado
    também a uma rede com rota de internet (`dse_net`).
  - `--cpus`, `--memory`, `--pids-limit` derivados do `budget` do WorkItem.
  - Label `dse.work_item_id=<id>` em todo container — usado tanto para
    idempotência de provisionamento quanto para descoberta em teardown/kill.
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

# S7-c (Fase 5): a imagem do sandbox precisa ter as ferramentas que rodam
# DENTRO dele via `docker exec` — git (finalize_pr push) e a toolchain do repo
# (ex.: node para o L1 `node --check` de um repo JS). A `dse-sandbox-base`
# (WSC-E3-T4b) traz git+node+Playwright. Configurável por env para não fixar.
DEFAULT_SANDBOX_IMAGE = os.environ.get("DSE_SANDBOX_IMAGE", "python:3.11-slim")
DEFAULT_NONROOT_USER = "10001:10001"

# Resource class -> defaults, usado quando o WorkItem.budget não especifica
# um valor explícito. Mantém paridade com o vocabulário de "resource class"
# usado nas métricas OTel (dse.resource_class).
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
    # Fase 3 (WSC-E3-T4b): tamanho do tmpfs de /tmp. O default de 64MB da
    # Fase 1 continua; o run de evidência @demo (chromium headless escreve
    # scratch/crashpad em /tmp) usa `budget={"tmp_mb": 256}`. Aditivo — não
    # muda nenhum comportamento existente.
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
    created_new: bool  # False quando reaproveitou container idempotentemente
    started_at: float = field(default_factory=time.time)


def _client() -> docker.DockerClient:
    return docker.from_env()


def ensure_sandbox_network(client: docker.DockerClient | None = None) -> str:
    """Cria (se necessário) a rede Docker interna sem rota de internet usada
    por todo sandbox. Idempotente."""
    client = client or _client()
    try:
        client.networks.get(SANDBOX_NETWORK_NAME)
    except NotFound:
        client.networks.create(
            SANDBOX_NETWORK_NAME,
            driver="bridge",
            internal=True,  # sem gateway de internet — propriedade central do isolamento
            check_duplicate=True,
            labels={LABEL_COMPONENT: "sandbox-runtime"},
        )
    return SANDBOX_NETWORK_NAME


def _container_name(work_item_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in work_item_id)
    return f"dse-sandbox-{safe}"


def find_existing_container(work_item_id: str, client: docker.DockerClient | None = None) -> Container | None:
    """Idempotência de provisionamento (WSC-E1-T3): procura por label antes de
    criar. Considera qualquer estado (running/exited) — reaproveita."""
    client = client or _client()
    # Filtra por work_item_id no daemon e por role no cliente (docker-py não
    # garante AND entre múltiplos valores da mesma chave 'label' de forma
    # portável entre versões do daemon).
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
        # Nenhum bind de docker.sock, nenhum --privileged, nenhum extra_hosts
        # apontando para o host — a única forma de sair da rede interna é o
        # egress-proxy, que também está anexado a `dse_sandbox_net`.
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
    """Usado pelo teste de chaos (WSC-E1-T4): mata o container no meio da
    tarefa, sem passar por teardown gracioso."""
    client = client or _client()
    try:
        c = client.containers.get(container_id)
        c.kill(signal=signal)
    except NotFound:
        pass


def teardown_container(container_id: str, client: docker.DockerClient | None = None) -> float:
    """Remove o container e retorna o runtime em minutos (para métrica OTel)."""
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
    """True se NENHUM mount do container referencia o socket Docker do host —
    usado pelo teste de isolamento (WSC-E1-T1)."""
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
