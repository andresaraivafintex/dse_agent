#!/usr/bin/env python3
""""Echo model" HTTP server — WSD-E1-T2.

A tiny, deterministic, dependency-free (stdlib only) OpenAI-compatible
provider, registered in LiteLLM as `eco/echo-model` (see
../litellm_config.yaml). It exists so the model-gateway can be tested
end-to-end without depending on any paid/external API (real Bedrock is not
available in this session — see README.md).

Contract: it implements the subset of `POST /v1/chat/completions` that LiteLLM
expects from a custom `openai/*` provider (with api_base pointing here). The
response is 100% deterministic as a function of the input: same input -> same
output, always, with no state, no network I/O, no call to any real LLM. That is
what makes it a good "smoke test double" for the gateway (WSD-E1-T1 "simulated
upgrade": bringing up a new LiteLLM version and comparing responses byte for
byte against this provider proves the proxy's routing/serialization did not
regress, independent of any real LLM's variability).

Usage: `python3 server.py` (port via env ECHO_PORT, default 9000).
"""
from __future__ import annotations

import hashlib
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("ECHO_PORT", "9000"))
# Phase 3 (WSD-E4-T1): the served "model" name is configurable so the second
# instance (intra-tier fallback, container dse_model_gateway_echo_b) is
# distinguishable from the primary in logs/responses.
MODEL_NAME = os.environ.get("ECHO_MODEL_NAME", "echo-model")

# Phase 3 (WSD-E4-T3, "quota exhaustion" chaos): if the last user message
# contains this marker, the server answers 429 in the OpenAI error shape — a
# DETERMINISTIC simulation (same input -> same output, no state, no clock) of a
# real provider's quota throttling (e.g. Bedrock ThrottlingException). It lets
# us prove the gateway's clean boundary (P6) under quota exhaustion without
# depending on a real, exhaustible quota.
QUOTA_EXHAUSTED_MARKER = "[[SIMULATE_QUOTA_EXHAUSTED]]"


def _deterministic_completion(messages: list[dict]) -> str:
    """Purely deterministic transform: no RNG, no clock, no external call. The
    same list of messages always produces the same text.
    """
    last_user = ""
    for m in reversed(messages or []):
        if m.get("role") == "user":
            last_user = str(m.get("content", ""))
            break
    return f"ECHO[{last_user[::-1]}]"


def _count_tokens(text: str) -> int:
    """Cheap deterministic count (whitespace split) — not a real tokenizer
    (bpe), documented as an approximation only for local cost/observability
    testing (see README)."""
    return max(1, len(text.split()))


def _deterministic_id(payload: bytes) -> str:
    return "echocmpl-" + hashlib.sha256(payload).hexdigest()[:24]


class EchoHandler(BaseHTTPRequestHandler):
    server_version = "DseEchoModel/1.0"

    def log_message(self, fmt, *args):  # silences noisy stdout in test runs
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

        # Deterministic chaos hook (WSD-E4-T3): simulated quota exhaustion.
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
            "created": 0,  # deterministic on purpose (do not use time.time())
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
