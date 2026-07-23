"""WSC-E2-T1: proxy default-deny real (asyncio, TCP de verdade) — encaminha
só hosts na allowlist; qualquer host fora dela é recusado e gera
`dse_audit.emit(action="egress_denied", ...)` — GRAVADO NO POSTGRES REAL
(nunca mockado; ver CONVENTIONS.md P8/regra de nunca mockar Postgres para
garantias de durabilidade/evidência)."""
from __future__ import annotations

import http.client

import psycopg2

from egress_proxy import Allowlist


def _request_via_proxy(proxy_port: int, target_host: str, target_port: int, path: str = "/") -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=5)
    conn.request("GET", f"http://{target_host}:{target_port}{path}")
    return conn.getresponse()


def test_allowed_host_is_forwarded(running_proxy_factory, upstream_server):
    allowlist = Allowlist.for_work_item(model_gateway_host="127.0.0.1", model_gateway_port=1)
    allowlist.entries.append(_entry_for_upstream(upstream_server))
    rp = running_proxy_factory(allowlist)

    resp = _request_via_proxy(rp.port, upstream_server["host"], upstream_server["port"])
    assert resp.status == 200
    assert resp.read() == b"ok"


def test_disallowed_host_gets_403_and_is_denied_locally(running_proxy_factory):
    allowlist = Allowlist.for_work_item(model_gateway_host="127.0.0.1", model_gateway_port=1)
    rp = running_proxy_factory(allowlist)

    resp = _request_via_proxy(rp.port, "not-on-the-allowlist.example", 80)
    assert resp.status == 403
    assert len(rp.proxy.denials) == 1
    assert rp.proxy.denials[0].host == "not-on-the-allowlist.example"


def test_disallowed_host_denial_is_audited_in_real_postgres(running_proxy_factory, work_item_id):
    allowlist = Allowlist.for_work_item(model_gateway_host="127.0.0.1", model_gateway_port=1)
    rp = running_proxy_factory(allowlist)

    resp = _request_via_proxy(rp.port, "audited-denial.example", 80)
    assert resp.status == 403

    conn = psycopg2.connect("postgresql://dse_app:dse_app_dev_only@localhost:5432/dse")
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT actor, details FROM audit_log
                 WHERE action = 'egress_denied'
                   AND work_item_id = %s
                   AND details->>'host' = 'audited-denial.example'
                 ORDER BY id DESC LIMIT 1
                """,
                (work_item_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    assert row is not None, "esperava uma linha real em audit_log para o egress negado"
    actor, details = row
    assert actor == "system:egress-proxy"
    assert details["host"] == "audited-denial.example"


def test_connect_tunnel_enforces_allowlist_too(running_proxy_factory, upstream_server):
    """CONNECT (túnel HTTPS opaco) também é sujeito à allowlist — não só o
    proxy HTTP em texto plano."""
    allowlist = Allowlist.for_work_item(model_gateway_host="127.0.0.1", model_gateway_port=1)
    allowlist.entries.append(_entry_for_upstream(upstream_server))
    rp = running_proxy_factory(allowlist)

    import socket

    with socket.create_connection(("127.0.0.1", rp.port), timeout=5) as s:
        s.sendall(b"CONNECT not-allowed.example:443 HTTP/1.1\r\nHost: not-allowed.example\r\n\r\n")
        response = s.recv(4096)
    assert b"403" in response


def _entry_for_upstream(upstream_server):
    from egress_proxy.allowlist import AllowlistEntry

    return AllowlistEntry(host=upstream_server["host"], port=upstream_server["port"], reason="test upstream")
