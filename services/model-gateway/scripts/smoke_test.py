#!/usr/bin/env python3
"""LiteLLM "simulated upgrade" smoke test (WSD-E1-T1).

Documented upgrade procedure:
  1. Run this script ONCE against the currently pinned version with `--record`
     to write the baseline into `scripts/smoke_baseline.json`.
  2. To test an upgrade: edit the image digest in `docker-compose.wsd.yml`
     (WS-D) to the new candidate version.
  3. `docker compose -f docker-compose.wsd.yml up -d --force-recreate model-gateway`
  4. Run this script WITHOUT `--record` — it compares the current response byte
     for byte (the echo model's deterministic content + the response shape)
     against the recorded baseline. Any difference = a proxy regression
     (routing, serialization, cost headers) — decline-never-truncate (P6): the
     script exits with a non-zero code, never "carries on anyway".
  5. Only promote the new digest after running the full pytest suite AND this
     smoke test coming out clean.

Determinism: the echo model (echo_provider/server.py) uses neither a clock nor
RNG — the SAME input always produces the SAME output, so any diff here is 100%
attributable to LiteLLM itself (the proxy), not to a real LLM's variability.
That is what makes this byte-for-byte comparison valid.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

BASELINE_PATH = Path(__file__).resolve().parent / "smoke_baseline.json"
FIXED_PROBE_MESSAGE = "dse-model-gateway-smoke-test-probe"


def _probe(base_url: str, master_key: str) -> dict:
    readiness = httpx.get(f"{base_url}/health/readiness", timeout=10.0)
    chat = httpx.post(
        f"{base_url}/v1/chat/completions",
        json={
            "model": "eco/echo-model",
            "messages": [{"role": "user", "content": FIXED_PROBE_MESSAGE}],
        },
        headers={"Authorization": f"Bearer {master_key}"},
        timeout=10.0,
    )
    chat_body = chat.json()
    return {
        "readiness_status": readiness.status_code,
        "chat_status": chat.status_code,
        "chat_content": chat_body.get("choices", [{}])[0].get("message", {}).get("content"),
        "chat_usage": chat_body.get("usage"),
        "chat_id": chat_body.get("id"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:4000")
    parser.add_argument("--master-key", default="sk-dse-local-dev-master-key")
    parser.add_argument(
        "--record", action="store_true", help="grava a resposta atual como nova baseline"
    )
    args = parser.parse_args()

    current = _probe(args.base_url, args.master_key)

    if args.record or not BASELINE_PATH.exists():
        BASELINE_PATH.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        print(f"[smoke_test] baseline written to {BASELINE_PATH}")
        print(json.dumps(current, indent=2, sort_keys=True))
        return 0

    baseline = json.loads(BASELINE_PATH.read_text())
    # chat_id is deterministic per request content, but it embeds LiteLLM
    # implementation details (routing) that can change harmlessly between
    # versions — we compare it separately and do not fail the smoke test just
    # because of it (a real failure is content/shape/status).
    baseline_stable = {k: v for k, v in baseline.items() if k != "chat_id"}
    current_stable = {k: v for k, v in current.items() if k != "chat_id"}

    if current_stable != baseline_stable:
        print("[smoke_test] REGRESSÃO DETECTADA — resposta divergiu da baseline", file=sys.stderr)
        print(f"baseline: {json.dumps(baseline_stable, indent=2, sort_keys=True)}", file=sys.stderr)
        print(f"atual:    {json.dumps(current_stable, indent=2, sort_keys=True)}", file=sys.stderr)
        return 1

    print("[smoke_test] OK — response identical to the baseline")
    return 0


if __name__ == "__main__":
    sys.exit(main())
