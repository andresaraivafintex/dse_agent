#!/usr/bin/env python3
"""Servidor HTTP "modelo eco" — WSD-E1-T2.

Um provider OpenAI-compatível minúsculo, determinístico e sem nenhuma
dependência externa (só stdlib), registrado no LiteLLM como
`eco/echo-model` (ver ../litellm_config.yaml). Existe para permitir testar o
model-gateway fim-a-fim sem depender de nenhuma API paga/externa (Bedrock
real não está disponível nesta sessão — ver README.md).

Contrato: implementa o subconjunto de `POST /v1/chat/completions` que o
LiteLLM espera de um provider `openai/*` custom (api_base apontando aqui).
A resposta é 100% determinística em função do input: mesma entrada -> mesma
saída sempre, sem estado, sem I/O de rede, sem chamada a nenhum LLM real.
Isso é o que faz dela um bom "smoke test double" para o gateway (WSD-E1-T1
"upgrade simulado": subir uma nova versão do LiteLLM e comparar respostas
byte-a-byte contra este provider prova que o roteamento/serialização do
proxy não regrediu, independente de qualquer variabilidade de um LLM real).

Uso: `python3 server.py` (porta via env ECHO_PORT, default 9000).
"""
from __future__ import annotations

import hashlib
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("ECHO_PORT", "9000"))
# Fase 3 (WSD-E4-T1): o nome do "modelo" servido é configurável para que a
# segunda instância (fallback intra-tier, container dse_model_gateway_echo_b)
# seja distinguível da primária em logs/respostas.
MODEL_NAME = os.environ.get("ECHO_MODEL_NAME", "echo-model")

# Fase 3 (WSD-E4-T3, chaos "exaustão de quota"): se a última mensagem de user
# contiver este marcador, o servidor responde 429 no shape de erro OpenAI —
# simulação DETERMINÍSTICA (mesma entrada -> mesma saída, sem estado, sem
# relógio) do throttling de quota de um provider real (ex.: Bedrock
# ThrottlingException). Permite provar a fronteira limpa (P6) do gateway sob
# quota exhaustion sem depender de uma quota real esgotável.
QUOTA_EXHAUSTED_MARKER = "[[SIMULATE_QUOTA_EXHAUSTED]]"


def _deterministic_completion(messages: list[dict]) -> str:
    """Transformação puramente determinística: sem RNG, sem relógio, sem
    chamada externa. A mesma lista de mensagens sempre produz o mesmo texto.
    """
    last_user = ""
    for m in reversed(messages or []):
        if m.get("role") == "user":
            last_user = str(m.get("content", ""))
            break
    return f"ECHO[{last_user[::-1]}]"


def _count_tokens(text: str) -> int:
    """Contagem determinística e barata (whitespace split) — não é um
    tokenizer real (bpe), documentado como aproximação apenas para fins de
    teste de custo/observabilidade local (ver README)."""
    return max(1, len(text.split()))


def _deterministic_id(payload: bytes) -> str:
    return "echocmpl-" + hashlib.sha256(payload).hexdigest()[:24]


class EchoHandler(BaseHTTPRequestHandler):
    server_version = "DseEchoModel/1.0"

    def log_message(self, fmt, *args):  # silencia stdout ruidoso em test runs
        pass

    def _send_json(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path in ("/health", "/v1/health"):
            self._send_json(200, {"status": "ok", "model": MODEL_NAME})
            return
        if self.path in ("/v1/models", "/models"):
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [{"id": MODEL_NAME, "object": "model", "owned_by": "dse-local"}],
                },
            )
            return
        self._send_json(404, {"error": "not_found"})

    def do_POST(self):
        if self.path not in ("/v1/chat/completions", "/chat/completions"):
            self._send_json(404, {"error": "not_found", "path": self.path})
            return

        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw_body or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid_json"})
            return

        messages = payload.get("messages", [])

        # Chaos hook determinístico (WSD-E4-T3): quota exhaustion simulada.
        last_user = ""
        for m in reversed(messages or []):
            if m.get("role") == "user":
                last_user = str(m.get("content", ""))
                break
        if QUOTA_EXHAUSTED_MARKER in last_user:
            self._send_json(
                429,
                {
                    "error": {
                        "message": f"simulated provider quota exhausted ({MODEL_NAME})",
                        "type": "rate_limit_error",
                        "param": None,
                        "code": "429",
                    }
                },
            )
            return

        completion_text = _deterministic_completion(messages)

        prompt_text = " ".join(str(m.get("content", "")) for m in messages)
        prompt_tokens = _count_tokens(prompt_text)
        completion_tokens = _count_tokens(completion_text)

        response = {
            "id": _deterministic_id(raw_body),
            "object": "chat.completion",
            "created": 0,  # determinístico de propósito (não usar time.time())
            "model": payload.get("model", MODEL_NAME),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": completion_text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }
        self._send_json(200, response)


def main() -> None:
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), EchoHandler)
    print(f"[echo-model] listening on :{PORT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
