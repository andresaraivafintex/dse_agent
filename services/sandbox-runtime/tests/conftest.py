from __future__ import annotations

import os
import uuid

import docker
import pytest


# Test DSN: prefers the app role (dse_app) but falls back to the `dse` superuser
# used locally by the foundation; both can see the WS-C tables (grants in
# 0004/0010). migrate runs as `dse`; the tests only need SELECT/INSERT.
_TEST_DSN = os.environ.get(
    "DSE_DATABASE_URL", "postgresql://dse:dse_dev_only@localhost:5432/dse"
)


@pytest.fixture(scope="session")
def pg_dsn():
    return _TEST_DSN


@pytest.fixture()
def pg_conn(pg_dsn):
    import psycopg2

    conn = psycopg2.connect(pg_dsn)
    try:
        yield conn
    finally:
        conn.close()

# Avoids the OTel console exporter (which runs on a periodic background thread
# and logs noise/"I/O on closed file" errors when the test process ends before
# the next export cycle) — tests that actually want to inspect metrics use
# `metrics.set_provider_for_test` with an explicit `InMemoryMetricReader`.
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
    # sandbox_runtime.activities reads the env var at import time; reload the
    # module-level value directly for the tests that use `_paths_for`.
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
