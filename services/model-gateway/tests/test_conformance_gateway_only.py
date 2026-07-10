"""WSD-E1-T4: prova que `model_gateway_client` nunca chama um provider
diretamente (Bedrock/anthropic/OpenAI SDKs) — o único caminho é o
model-gateway (LiteLLM), na URL única `settings.gateway_base_url()`.

Duas provas complementares:
  1. Estática: nenhum módulo do pacote importa um SDK de provider.
  2. Dinâmica: instrumentamos `httpx` para gravar toda URL chamada durante um
     fluxo real (mint -> chat_completion -> revoke) e verificamos que 100%
     delas batem no host:porta do gateway — nenhuma bate direto num
     "provider" (aqui, o processo do modelo eco, que representa o papel que
     o Bedrock/PrivateLink teria em produção).
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
    """Análise estática (AST, não regex) de todo .py do pacote: nenhum
    `import boto3` / `import anthropic` / `import openai` — o cliente só
    fala HTTP com o gateway."""
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
    assert not offending, f"SDK de provider importado diretamente: {offending}"


def test_all_http_calls_go_through_the_gateway_base_url(unique_ids, monkeypatch):
    """Prova dinâmica: intercepta TODA chamada httpx (a única lib HTTP usada
    pelo cliente) durante um fluxo mint -> chat_completion -> revoke, e
    verifica que cada uma foi para host:porta do gateway — nunca para
    qualquer outro endereço (ex.: o modelo eco diretamente, que nem é
    alcançável do host — não tem porta publicada no docker-compose.wsd.yml
    — mas a prova aqui é sobre o que o CLIENTE tenta fazer, por construção)."""
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
        ), f"chamada saiu do gateway: {url}"
