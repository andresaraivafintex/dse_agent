from __future__ import annotations

import os
import uuid

import docker
import pytest

from sandbox_runtime import docker_driver

# Evita o exporter de console do OTel (que roda num thread periódico em
# background e loga barulho/erros de "I/O on closed file" quando o processo
# de teste termina antes do próximo ciclo de export) — os testes que querem
# inspecionar métricas de verdade usam `metrics.set_provider_for_test` com um
# `InMemoryMetricReader` explicitamente.
os.environ.setdefault("DSE_OTEL_DISABLE_CONSOLE_EXPORT", "1")


@pytest.fixture(scope="session")
def docker_client():
    client = docker.from_env()
    client.ping()
    return client


@pytest.fixture()
def work_item_id():
    return f"wi-{uuid.uuid4().hex[:12]}"


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "dse-sandboxes"
    d.mkdir()
    monkeypatch.setenv("DSE_SANDBOX_STATE_DIR", str(d))
    # sandbox_runtime.activities lê a env var no import; recarrega o valor
    # do módulo diretamente para os testes que usam `_paths_for`.
    import sandbox_runtime.activities as activities_mod

    activities_mod._STATE_DIR = str(d)
    yield str(d)


def _cleanup_container(client: docker.DockerClient, name_or_id: str) -> None:
    try:
        c = client.containers.get(name_or_id)
        c.remove(force=True)
    except docker.errors.NotFound:
        pass


@pytest.fixture()
def cleanup_containers(docker_client):
    created: list[str] = []
    yield created
    for cid in created:
        _cleanup_container(docker_client, cid)
