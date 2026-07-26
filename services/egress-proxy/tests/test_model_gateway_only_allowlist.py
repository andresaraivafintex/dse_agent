"""WSC-E2-T3: the model-gateway (WS-D, port 4000)/LiteLLM is the ONLY allowlist
entry for model calls — a direct call to an external provider from inside the
sandbox must be blocked and audited."""
from __future__ import annotations

import http.client

import psycopg2

from egress_proxy import Allowlist


KNOWN_PROVIDER_HOSTS = [
    "api.anthropic.com",
    "api.openai.com",
    "bedrock-runtime.us-east-1.amazonaws.com",
    "generativelanguage.googleapis.com",
]


def _request_via_proxy(port: int, host: str, target_port: int = 443, path: str = "/v1/messages"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", f"http://{host}:{target_port}{path}")
    return conn.getresponse()


def test_model_gateway_is_the_only_model_call_allowlist_entry():
    allowlist = Allowlist.for_work_item(model_gateway_host="model-gateway.internal", model_gateway_port=4000)
    model_gateway_entries = [e for e in allowlist.entries if e.category == "model_gateway"]
    assert len(model_gateway_entries) == 1
    assert model_gateway_entries[0].host == "model-gateway.internal"
    assert model_gateway_entries[0].port == 4000

    for provider_host in KNOWN_PROVIDER_HOSTS:
        assert not allowlist.is_allowed(provider_host, 443), (
            f"{provider_host} should not be on the allowlist — only the model-gateway may be called for inference"
        )


def test_direct_provider_call_is_blocked_and_audited(running_proxy_factory, work_item_id):
    allowlist = Allowlist.for_work_item(model_gateway_host="model-gateway.internal", model_gateway_port=4000)
    rp = running_proxy_factory(allowlist)

    for provider_host in KNOWN_PROVIDER_HOSTS:
        resp = _request_via_proxy(rp.port, provider_host)
        assert resp.status == 403, f"a direct call to {provider_host} should have been blocked"

    conn = psycopg2.connect("postgresql://dse_app:dse_app_dev_only@localhost:5432/dse")
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT details->>'host' FROM audit_log
                 WHERE action = 'egress_denied' AND work_item_id = %s
                 ORDER BY id
                """,
                (work_item_id,),
            )
            denied_hosts = {row[0] for row in cur.fetchall()}
    finally:
        conn.close()

    assert set(KNOWN_PROVIDER_HOSTS) <= denied_hosts, "expected a denial audit row for every external provider attempted"


def test_model_gateway_host_itself_is_allowed(running_proxy_factory):
    from egress_proxy.allowlist import AllowlistEntry

    allowlist = Allowlist.for_work_item(model_gateway_host="127.0.0.1", model_gateway_port=1)
    # replace the default model-gateway entry with one that actually points at a
    # real server in this test.
    allowlist.entries = [e for e in allowlist.entries if e.category != "model_gateway"]

    import http.server
    import socket
    import threading

    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    port = _free_port()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

    httpd = http.server.HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        allowlist.entries.append(AllowlistEntry(host="127.0.0.1", port=port, category="model_gateway"))
        rp = running_proxy_factory(allowlist)
        resp = _request_via_proxy(rp.port, "127.0.0.1", target_port=port, path="/")
        assert resp.status == 200
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
