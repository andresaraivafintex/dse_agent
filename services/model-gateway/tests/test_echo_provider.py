"""Testa o servidor "modelo eco" (echo_provider/server.py) isoladamente,
sem Docker — sobe o mesmo processo HTTP num thread local em porta efêmera.
Prova que a lógica é determinística (WSD-E1-T2: pré-requisito para o
"upgrade simulado" comparar respostas byte-a-byte)."""
from __future__ import annotations

import importlib.util
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

_SERVER_PATH = Path(__file__).resolve().parents[1] / "echo_provider" / "server.py"


def _load_server_module():
    spec = importlib.util.spec_from_file_location("dse_echo_provider_server", _SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def echo_server():
    module = _load_server_module()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), module.EchoHandler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
    httpd.server_close()


def test_health(echo_server):
    resp = httpx.get(f"{echo_server}/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_chat_completion_is_deterministic(echo_server):
    body = {"model": "echo-model", "messages": [{"role": "user", "content": "hello dse"}]}
    resp1 = httpx.post(f"{echo_server}/v1/chat/completions", json=body)
    resp2 = httpx.post(f"{echo_server}/v1/chat/completions", json=body)
    assert resp1.status_code == 200
    assert resp1.json() == resp2.json()  # mesma entrada -> mesma saída, sempre


def test_chat_completion_shape_and_content(echo_server):
    body = {"model": "echo-model", "messages": [{"role": "user", "content": "abc"}]}
    resp = httpx.post(f"{echo_server}/v1/chat/completions", json=body)
    payload = resp.json()
    assert payload["object"] == "chat.completion"
    assert payload["choices"][0]["message"]["content"] == "ECHO[cba]"
    assert payload["choices"][0]["message"]["role"] == "assistant"
    assert payload["choices"][0]["finish_reason"] == "stop"
    usage = payload["usage"]
    assert usage["prompt_tokens"] == 1
    assert usage["completion_tokens"] == 1
    assert usage["total_tokens"] == 2


def test_different_input_changes_output(echo_server):
    body_a = {"model": "echo-model", "messages": [{"role": "user", "content": "aaa"}]}
    body_b = {"model": "echo-model", "messages": [{"role": "user", "content": "bbb bbb"}]}
    resp_a = httpx.post(f"{echo_server}/v1/chat/completions", json=body_a).json()
    resp_b = httpx.post(f"{echo_server}/v1/chat/completions", json=body_b).json()
    assert resp_a["choices"][0]["message"]["content"] != resp_b["choices"][0]["message"]["content"]
    assert resp_a["id"] != resp_b["id"]


def test_unknown_route_404(echo_server):
    resp = httpx.post(f"{echo_server}/v1/embeddings", json={})
    assert resp.status_code == 404
