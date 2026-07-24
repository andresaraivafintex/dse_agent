"""Helpers compartilhados dos testes de chaos/failover do WS-D (Fase 3).

Chaos REAL: derrubamos containers do próprio WS-D (`docker stop`) — nunca a
infra compartilhada da fundação (Postgres/Temporal/Redis/Vault) nem serviços
de outros workstreams. Todo teste que derruba um eco DEVE restaurar em
`finally` e esperar o primário voltar a servir (`ensure_primary_serving`)
para não vazar estado degradado para outros testes/agentes em paralelo.
"""
from __future__ import annotations

import os
import subprocess
import time

import httpx

PRIMARY_ECHO_CONTAINER = "dse_model_gateway_echo"
FALLBACK_ECHO_CONTAINER = "dse_model_gateway_echo_b"
PRIMARY_API_BASE = "http://model-gateway-echo:9000"
FALLBACK_API_BASE = "http://model-gateway-echo-b:9000"

ECHO_MODEL = "eco/echo-model"
ECHO_MODEL_B = "eco/echo-model-b"


def _gateway_base() -> str:
    return os.environ.get("DSE_MODEL_GATEWAY_BASE_URL", "http://localhost:4000")


def _master_key() -> str:
    return os.environ.get("DSE_LITELLM_MASTER_KEY", "sk-dse-local-dev-master-key")


def docker(*args: str) -> None:
    subprocess.run(["docker", *args], check=True, capture_output=True, timeout=60)


def stop_container(name: str) -> None:
    docker("stop", name)


def start_container(name: str) -> None:
    docker("start", name)


def raw_completion(content: str, *, model: str = ECHO_MODEL, key: str | None = None) -> httpx.Response:
    """Chamada crua ao gateway (sem passar pelo client instrumentado) — usada
    pelos helpers de saúde para não poluir ledger/audit dos testes."""
    return httpx.post(
        f"{_gateway_base()}/v1/chat/completions",
        headers={"Authorization": f"Bearer {key or _master_key()}"},
        json={"model": model, "messages": [{"role": "user", "content": content}]},
        timeout=15.0,
    )


def ensure_primary_serving(timeout_seconds: float = 60.0) -> None:
    """Espera o deployment PRIMÁRIO voltar a servir (container de pé + fora do
    cooldown do router). Falha alto se não voltar — deixar o gateway degradado
    quebraria os testes seguintes e os outros agentes em paralelo."""
    deadline = time.monotonic() + timeout_seconds
    last: str | None = None
    while time.monotonic() < deadline:
        try:
            resp = raw_completion("healthcheck-primary")
            last = resp.headers.get("x-litellm-model-api-base")
            if resp.status_code == 200 and last == PRIMARY_API_BASE:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1.0)
    raise AssertionError(
        f"primário nunca voltou a servir em {timeout_seconds}s (último api_base={last!r})"
    )


def wait_until_fallback_serves(timeout_seconds: float = 60.0) -> None:
    """Após derrubar o primário, espera o router ESTABILIZAR servindo pelo
    FALLBACK (echo-b) — elimina a corrida entre `docker stop` e a chamada
    instrumentada do teste (o primário podia ainda estar servindo, então não
    havia degradação nenhuma). Usa raw_completion (master key direta), que NÃO
    escreve no ledger/audit dos testes. Depois disto o primário está em
    cooldown, então a chamada instrumentada vai DIRETO ao fallback
    (attempted_fallbacks=0) — degradação por cooldown, detectada via
    DSE_FALLBACK_API_BASES."""
    deadline = time.monotonic() + timeout_seconds
    last: str | None = None
    while time.monotonic() < deadline:
        try:
            resp = raw_completion("healthcheck-fallback")
            last = resp.headers.get("x-litellm-model-api-base")
            if resp.status_code == 200 and last == FALLBACK_API_BASE:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise AssertionError(
        f"fallback nunca assumiu em {timeout_seconds}s (último api_base={last!r})"
    )
