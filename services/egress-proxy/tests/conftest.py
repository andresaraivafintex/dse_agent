from __future__ import annotations

import asyncio
import socket
import threading
import uuid

import pytest

from egress_proxy import Allowlist, EgressProxy
from egress_proxy.credentials import CredentialBroker


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RunningProxy:
    """Runs a real `EgressProxy` (asyncio) in a background thread, with its own
    event loop — this lets the tests use ordinary synchronous
    sockets/`http.client` against `localhost:port`, with no pytest-asyncio."""

    def __init__(self, allowlist: Allowlist, *, tenant_id: str, work_item_id: str, credential_broker=None):
        self.port = free_port()
        self._loop = asyncio.new_event_loop()
        self.proxy = EgressProxy(
            allowlist, tenant_id=tenant_id, work_item_id=work_item_id, credential_broker=credential_broker
        )
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._started = threading.Event()
        self._thread.start()
        self._started.wait(timeout=5)

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self.proxy.start(host="127.0.0.1", port=self.port))
        self._started.set()
        self._loop.run_forever()

    def stop(self):
        async def _stop():
            await self.proxy.stop()

        fut = asyncio.run_coroutine_threadsafe(_stop(), self._loop)
        fut.result(timeout=5)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._loop.close()


@pytest.fixture()
def work_item_id():
    return f"wi-{uuid.uuid4().hex[:12]}"


@pytest.fixture()
def upstream_server():
    """Real, simple HTTP server (`http.server`) running in the background, used
    as the "allowed host" in the allowlist tests."""
    import http.server

    port = free_port()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):  # silences the test stdout
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

    httpd = http.server.HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield {"host": "127.0.0.1", "port": port}
    httpd.shutdown()
    thread.join(timeout=5)


@pytest.fixture()
def credential_broker():
    return CredentialBroker()


@pytest.fixture()
def running_proxy_factory(work_item_id, credential_broker):
    created: list[RunningProxy] = []

    def _make(allowlist: Allowlist) -> RunningProxy:
        rp = RunningProxy(allowlist, tenant_id="tenant-test", work_item_id=work_item_id, credential_broker=credential_broker)
        created.append(rp)
        return rp

    yield _make

    for rp in created:
        rp.stop()
