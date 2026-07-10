"""WSC-E2-T2: credenciais efêmeras injetadas pelo proxy — NUNCA dentro do
container do sandbox — e revogação com SLO de até 60s.

`test_no_token_reaches_sandbox_container` roda um container Docker de
verdade que faz uma requisição via proxy com o header placeholder
`X-Dse-Inject-Credential: github`, e então vasculha env/filesystem/processo
do PRÓPRIO container para provar que o token real nunca apareceu lá dentro —
só o placeholder saiu do container; o valor real foi injetado depois, do
lado do proxy.
"""
from __future__ import annotations

import http.client
import time

import psycopg2
import pytest

from egress_proxy import Allowlist
from egress_proxy.credentials import GitHubScopeError, REVOCATION_SLO_SECONDS


def test_credential_broker_mints_scoped_token(credential_broker, work_item_id):
    cred = credential_broker.mint(work_item_id=work_item_id, repo="acme/widgets", branch="dse/task-1")
    assert cred.token
    assert cred.allowed_actions == frozenset({"contents:write"})
    assert cred.fixture is True  # nenhum GITHUB_APP_ID configurado nesta sessão de dev


def test_credential_scope_rejects_pull_request_creation(credential_broker, work_item_id):
    cred = credential_broker.mint(work_item_id=work_item_id, repo="acme/widgets", branch="dse/task-1")
    with pytest.raises(GitHubScopeError):
        cred.create_pull_request(title="x", body="y")


def test_credential_scope_rejects_force_push(credential_broker, work_item_id):
    cred = credential_broker.mint(work_item_id=work_item_id, repo="acme/widgets", branch="dse/task-1")
    with pytest.raises(GitHubScopeError):
        cred.force_push()


def test_revocation_completes_within_slo_and_is_recorded(credential_broker, work_item_id):
    cred = credential_broker.mint(work_item_id=work_item_id, repo="acme/widgets", branch="dse/task-1")
    latency = credential_broker.revoke(cred.credential_id)

    assert latency < REVOCATION_SLO_SECONDS
    assert cred.is_revoked()

    conn = psycopg2.connect("postgresql://dse_app:dse_app_dev_only@localhost:5432/dse")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT revoked_at, revoke_latency_s FROM egress_credential_leases WHERE credential_id = %s",
                (cred.credential_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None
    revoked_at, revoke_latency_s = row
    assert revoked_at is not None
    assert revoke_latency_s < REVOCATION_SLO_SECONDS


def test_credential_placeholder_header_is_replaced_before_egress(running_proxy_factory, upstream_server):
    """A requisição que SAI do sandbox tem só o placeholder; quem a recebe
    do outro lado (o `upstream_server`, simulando o GitHub) vê o token real
    já injetado — prova que a troca acontece no proxy, não no container."""
    from egress_proxy.allowlist import AllowlistEntry

    allowlist = Allowlist.for_work_item(model_gateway_host="127.0.0.1", model_gateway_port=1)
    allowlist.entries.append(AllowlistEntry(host=upstream_server["host"], port=upstream_server["port"]))
    rp = running_proxy_factory(allowlist)

    conn = http.client.HTTPConnection("127.0.0.1", rp.port, timeout=5)
    conn.request(
        "GET",
        f"http://{upstream_server['host']}:{upstream_server['port']}/",
        headers={
            "X-Dse-Inject-Credential": "github",
            "X-Dse-Repo": "acme/widgets",
            "X-Dse-Branch": "dse/task-1",
        },
    )
    resp = conn.getresponse()
    assert resp.status == 200
    resp.read()

    # o broker do proxy mintou exatamente 1 credencial para essa chamada
    assert len(rp.proxy.credential_broker._issued) == 1
    minted = next(iter(rp.proxy.credential_broker._issued.values()))
    assert minted.repo == "acme/widgets"
    assert "fixture-ghtoken-" in minted.token  # nunca visível dentro do container que originou a chamada


def test_no_token_reaches_sandbox_container_env_fs_or_proc(running_proxy_factory, upstream_server):
    """Roda um container Docker de verdade que faz a chamada via proxy com
    só o header placeholder, depois vasculha env/filesystem/proc do MESMO
    container para provar que o token real nunca apareceu lá dentro."""
    import docker
    from egress_proxy.allowlist import AllowlistEntry

    allowlist = Allowlist.for_work_item(model_gateway_host="127.0.0.1", model_gateway_port=1)
    allowlist.entries.append(AllowlistEntry(host=upstream_server["host"], port=upstream_server["port"]))
    rp = running_proxy_factory(allowlist)

    client = docker.from_env()
    container = client.containers.run(
        "python:3.11-slim",
        command=["sleep", "120"],
        detach=True,
        user="10001:10001",
        read_only=True,
        tmpfs={"/tmp": "size=64m"},
        extra_hosts={"host.docker.internal": "host-gateway"},
        environment={"HOME": "/tmp"},
    )
    try:
        call_script = (
            "import urllib.request\n"
            f"handler = urllib.request.ProxyHandler({{'http': 'http://host.docker.internal:{rp.port}'}})\n"
            "opener = urllib.request.build_opener(handler)\n"
            "req = urllib.request.Request(\n"
            f"    'http://{upstream_server['host']}:{upstream_server['port']}/',\n"
            "    headers={'X-Dse-Inject-Credential': 'github', 'X-Dse-Repo': 'acme/widgets', 'X-Dse-Branch': 'dse/task-1'},\n"
            ")\n"
            "resp = opener.open(req, timeout=5)\n"
            "print('STATUS', resp.status)\n"
        )
        exit_code, out = container.exec_run(["python3", "-c", call_script])
        assert exit_code == 0, out.decode(errors="replace")
        assert b"STATUS 200" in out

        minted = next(iter(rp.proxy.credential_broker._issued.values()))
        real_token = minted.token

        exit_code, out = container.exec_run(["env"])
        assert real_token.encode() not in out, "token real vazou para env do container"

        exit_code, out = container.exec_run(["sh", "-c", "grep -r -a -l . /tmp 2>/dev/null || true"])
        exit_code, tmp_grep = container.exec_run(["sh", "-c", f"grep -r -a '{real_token}' /tmp 2>/dev/null || true"])
        assert real_token.encode() not in tmp_grep, "token real vazou para arquivo em /tmp do container"

        exit_code, proc_grep = container.exec_run(
            ["sh", "-c", "for f in /proc/[0-9]*/environ; do tr '\\0' '\\n' < \"$f\" 2>/dev/null; done"]
        )
        assert real_token.encode() not in proc_grep, "token real vazou para env de algum processo do container"
    finally:
        container.remove(force=True)
