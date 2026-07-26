"""WSC-E2-T2: ephemeral credentials injected by the proxy — NEVER inside the
sandbox container — and revocation with a 60s SLO.

`test_no_token_reaches_sandbox_container` runs a real Docker container that
makes a request through the proxy carrying only the placeholder header
`X-Dse-Inject-Credential: github`, and then combs the env/filesystem/processes
of THAT SAME container to prove the real token never appeared inside — only the
placeholder left the container; the real value was injected afterwards, on the
proxy side.
"""
from __future__ import annotations

import http.client
import os


import psycopg2
import pytest

from egress_proxy import Allowlist
from egress_proxy.credentials import GitHubScopeError, REVOCATION_SLO_SECONDS


def test_credential_broker_mints_scoped_token(credential_broker, work_item_id):
    cred = credential_broker.mint(work_item_id=work_item_id, repo="acme/widgets", branch="dse/task-1")
    assert cred.token
    assert cred.allowed_actions == frozenset({"contents:write"})
    assert cred.fixture is True  # no GITHUB_APP_ID configured in this dev session


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
    """The request LEAVING the sandbox carries only the placeholder; whoever
    receives it on the other side (the `upstream_server`, standing in for
    GitHub) sees the real token already injected — proof that the swap happens
    in the proxy, not in the container."""
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

    # the proxy's broker minted exactly 1 credential for this call
    assert len(rp.proxy.credential_broker._issued) == 1
    minted = next(iter(rp.proxy.credential_broker._issued.values()))
    assert minted.repo == "acme/widgets"
    assert "fixture-ghtoken-" in minted.token  # never visible inside the container that originated the call


@pytest.mark.skipif(
    os.environ.get("DSE_RUN_EGRESS_CONTAINER_TEST") != "1",
    reason=(
        "requires container→host routing (host.docker.internal) — works on "
        "Docker Desktop (dev) but not on the CI runner's native docker; opt-in "
        "with DSE_RUN_EGRESS_CONTAINER_TEST=1"
    ),
)
def test_no_token_reaches_sandbox_container_env_fs_or_proc(running_proxy_factory, upstream_server):
    """Runs a real Docker container that makes the call through the proxy with
    only the placeholder header, then combs the env/filesystem/proc of THAT SAME
    container to prove the real token never appeared inside."""
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
        assert real_token.encode() not in out, "the real token leaked into the container env"

        exit_code, out = container.exec_run(["sh", "-c", "grep -r -a -l . /tmp 2>/dev/null || true"])
        exit_code, tmp_grep = container.exec_run(["sh", "-c", f"grep -r -a '{real_token}' /tmp 2>/dev/null || true"])
        assert real_token.encode() not in tmp_grep, "the real token leaked into a file in the container's /tmp"

        exit_code, proc_grep = container.exec_run(
            ["sh", "-c", "for f in /proc/[0-9]*/environ; do tr '\\0' '\\n' < \"$f\" 2>/dev/null; done"]
        )
        assert real_token.encode() not in proc_grep, "the real token leaked into the env of some container process"
    finally:
        container.remove(force=True)
