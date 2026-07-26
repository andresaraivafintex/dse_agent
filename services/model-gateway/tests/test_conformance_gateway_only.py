"""WSD-E1-T4: proves `model_gateway_client` never calls a provider directly
(Bedrock/anthropic/OpenAI SDKs) — the only path is the model-gateway (LiteLLM),
at the single URL `settings.gateway_base_url()`.

Two complementary proofs:
  1. Static: no module in the package imports a provider SDK.
  2. Dynamic: we instrument `httpx` to record every URL called during a real
     flow (mint -> chat_completion -> revoke) and check that 100% of them hit
     the gateway's host:port — none hits a "provider" directly (here, the echo
     model process, which stands in for the role Bedrock/PrivateLink would play
     in production).
"""
from __future__ import annotations

import ast
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dse_contracts.gateway_contract import GatewayCallHeaders, Stage
from model_gateway_client import chat_completion, mint_virtual_key, revoke_virtual_key
from model_gateway_client import settings as gw_settings

_PACKAGE_DIR = Path(__file__).resolve().parents[1] / "model_gateway_client"
_FORBIDDEN_SDK_IMPORTS = {"boto3", "anthropic", "openai"}


def test_no_provider_sdk_imported_anywhere_in_package():
    """Static analysis (AST, not regex) of every .py in the package: no
    `import boto3` / `import anthropic` / `import openai` — the client only
    speaks HTTP to the gateway."""
    offending: list[str] = []
    for py_file in _PACKAGE_DIR.rglob("*.py"):
        tree = ast.parse(py_file.read_text(), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            else:
                continue
            hit = _FORBIDDEN_SDK_IMPORTS & set(names)
            if hit:
                offending.append(f"{py_file.name}: {hit}")
    assert not offending, f"a provider SDK is imported directly: {offending}"


def test_all_http_calls_go_through_the_gateway_base_url(unique_ids, monkeypatch):
    """Dynamic proof: intercepts EVERY httpx call (the only HTTP lib the client
    uses) during a mint -> chat_completion -> revoke flow, and checks each one
    went to the gateway's host:port — never to any other address (e.g. the echo
    model directly, which is not even reachable from the host — it publishes no
    port in docker-compose.wsd.yml — but the proof here is about what the
    CLIENT attempts, by construction)."""
    called_urls: list[str] = []

    real_post = httpx.post

    def spy_post(url, *args, **kwargs):
        called_urls.append(str(url))
        return real_post(url, *args, **kwargs)

    monkeypatch.setattr(httpx, "post", spy_post)

    tenant_id = unique_ids["tenant_id"]
    work_item_id = unique_ids["work_item_id"]
    key = mint_virtual_key(tenant_id, work_item_id, Stage.coder, models=["eco/echo-model"])
    headers = GatewayCallHeaders(tenant_id=tenant_id, work_item_id=work_item_id, stage=Stage.coder)
    chat_completion(
        headers=headers, virtual_key=key, model="eco/echo-model", messages=[{"role": "user", "content": "hi"}]
    )
    revoke_virtual_key(key)

    assert len(called_urls) == 3  # /key/generate, /v1/chat/completions, /key/delete

    gateway = urlparse(gw_settings.gateway_base_url())
    for url in called_urls:
        parsed = urlparse(url)
        assert (parsed.scheme, parsed.hostname, parsed.port) == (
            gateway.scheme,
            gateway.hostname,
            gateway.port,
        ), f"a call left the gateway: {url}"
