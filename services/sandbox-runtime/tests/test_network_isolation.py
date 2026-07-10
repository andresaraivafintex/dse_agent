"""WSC-E1-T1: sandbox rootless, sem docker.sock, sem escalada a root, e sem
acesso à internet exceto rota para o egress-proxy próprio.

Topologia real criada neste teste (nada mockado — containers Docker de
verdade, rede Docker de verdade):

  dse-test-upstream (só em dse_net)      <-- "internet" simulada
        ^
        | (dse_net)
  dse-egress-proxy-test (dse_net + dse_sandbox_net)  <-- ponte controlada
        ^
        | (dse_sandbox_net, internal=True — sem gateway de internet)
  <sandbox sob teste>  (só em dse_sandbox_net)
"""
from __future__ import annotations

import time
import uuid

import docker
import pytest

from sandbox_runtime import docker_driver
from sandbox_runtime.activities import ProvisionSandboxInput, TeardownSandboxInput, provision_sandbox, teardown_sandbox
import asyncio
import os


EGRESS_PROXY_SRC = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "egress-proxy")
)


def _exec(container, cmd: list[str]) -> tuple[int, str]:
    exit_code, output = container.exec_run(cmd, demux=False)
    return exit_code, output.decode(errors="replace")


@pytest.fixture()
def isolation_topology(docker_client, work_item_id):
    suffix = uuid.uuid4().hex[:8]
    upstream_name = f"dse-test-upstream-{suffix}"
    proxy_name = f"dse-egress-proxy-test-{suffix}"

    docker_driver.ensure_sandbox_network(docker_client)

    upstream = docker_client.containers.run(
        "python:3.11-slim",
        name=upstream_name,
        command=["python", "-m", "http.server", "8000"],
        detach=True,
        network="dse_net",
        labels={"dse.component": "sandbox-runtime-test", "dse.role": "test-upstream"},
    )

    proxy = docker_client.containers.run(
        "python:3.11-slim",
        name=proxy_name,
        command=["python", "-m", "egress_proxy.server"],
        detach=True,
        working_dir="/app",
        environment={
            "PYTHONPATH": "/app",
            "DSE_EGRESS_PORT": "8806",
            "DSE_EGRESS_TENANT_ID": "tenant-isolation-test",
            "DSE_EGRESS_WORK_ITEM_ID": work_item_id,
            "DSE_EGRESS_ALLOW_HOSTS": f"{upstream_name}:8000",
            "DSE_EGRESS_MODEL_GATEWAY_HOST": "model-gateway-does-not-exist",
        },
        volumes={EGRESS_PROXY_SRC: {"bind": "/app", "mode": "ro"}},
        network="dse_net",
        labels={"dse.component": "sandbox-runtime-test", "dse.role": "test-egress-proxy"},
    )
    # segunda rede: dse_sandbox_net (internal) — ponte controlada
    docker_client.networks.get(docker_driver.SANDBOX_NETWORK_NAME).connect(proxy)

    # espera os servidores subirem
    for _ in range(30):
        proxy.reload()
        upstream.reload()
        if proxy.status == "running" and upstream.status == "running":
            break
        time.sleep(0.5)
    time.sleep(1.5)  # margem para o asyncio server abrir o socket de escuta

    yield {"upstream_name": upstream_name, "proxy_name": proxy_name, "upstream": upstream, "proxy": proxy}

    for c in (upstream, proxy):
        try:
            c.remove(force=True)
        except docker.errors.NotFound:
            pass


def test_sandbox_isolation_and_egress_proxy_only_route(isolation_topology, docker_client, work_item_id, state_dir):
    proxy_name = isolation_topology["proxy_name"]
    upstream_name = isolation_topology["upstream_name"]

    handle = asyncio.run(
        provision_sandbox(ProvisionSandboxInput(work_item_id=work_item_id, tenant_id="tenant-isolation-test"))
    )
    sandbox = docker_client.containers.get(handle.container_id)

    try:
        # --- T1a: sem docker.sock, sem root ----------------------------------
        assert docker_driver.inspect_no_docker_sock(handle.container_id)
        user = docker_driver.inspect_non_root_user(handle.container_id)
        assert user not in ("", "root", "0", "0:0")

        exit_code, out = _exec(sandbox, ["id", "-u"])
        assert exit_code == 0
        assert out.strip() != "0", f"processo rodando como root dentro do sandbox: id -u = {out!r}"

        # --- T1b: nenhum host externo alcançável -------------------------------
        exit_code, out = _exec(
            sandbox,
            [
                "python3",
                "-c",
                "import urllib.request,sys\n"
                "try:\n"
                "    urllib.request.urlopen('http://example.com', timeout=4)\n"
                "    sys.exit(1)\n"  # se conseguiu, FALHA o teste de isolamento
                "except Exception as e:\n"
                "    print('BLOCKED:', type(e).__name__)\n"
                "    sys.exit(0)\n",
            ],
        )
        assert exit_code == 0, f"sandbox conseguiu alcançar host externo direto — isolamento quebrado: {out}"
        assert "BLOCKED" in out

        # --- T1c: só o egress-proxy é alcançável, e só encaminha allowlist -----
        proxy_script = (
            "import urllib.request\n"
            "handler = urllib.request.ProxyHandler({{'http': 'http://{proxy}:8806'}})\n"
            "opener = urllib.request.build_opener(handler)\n"
        ).format(proxy=proxy_name)

        allowed_script = proxy_script + (
            f"resp = opener.open('http://{upstream_name}:8000/', timeout=5)\n"
            "print('ALLOWED_STATUS', resp.status)\n"
        )
        exit_code, out = _exec(sandbox, ["python3", "-c", allowed_script])
        assert exit_code == 0, out
        assert "ALLOWED_STATUS 200" in out, f"host permitido via egress-proxy deveria ter passado: {out}"

        denied_script = proxy_script + (
            "import urllib.error\n"
            "try:\n"
            "    opener.open('http://example.com/', timeout=5)\n"
            "    print('UNEXPECTED_SUCCESS')\n"
            "except urllib.error.HTTPError as e:\n"
            "    print('DENIED_STATUS', e.code)\n"
        )
        exit_code, out = _exec(sandbox, ["python3", "-c", denied_script])
        assert exit_code == 0, out
        assert "DENIED_STATUS 403" in out, f"host fora da allowlist deveria ter sido recusado com 403: {out}"

    finally:
        asyncio.run(teardown_sandbox(TeardownSandboxInput(work_item_id=work_item_id, tenant_id="tenant-isolation-test")))


# Nota: a prova de que `egress_denied` grava uma linha REAL em `audit_log`
# (P8) está em `services/egress-proxy/tests/test_allowlist_and_audit.py`,
# rodando `EgressProxy` in-process (no venv que tem `dse_audit`/`psycopg2`
# instalados) contra o Postgres real — não faz sentido reproduzir aqui
# dentro do container "pelado" `python:3.11-slim` usado para o teste de
# isolamento de rede, que deliberadamente NÃO tem `dse_audit`/`psycopg2`
# instalados (é o cenário de container mínimo, sem `pip install`, descrito
# no README). Lá dentro o proxy cai para o fallback de log local — o mesmo
# código, testado em outro lugar com o caminho feliz do Postgres real.
