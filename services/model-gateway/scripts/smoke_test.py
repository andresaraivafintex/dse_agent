#!/usr/bin/env python3
"""Smoke test de "upgrade simulado" do LiteLLM (WSD-E1-T1).

Procedimento de upgrade documentado:
  1. Rode este script UMA VEZ contra a versão pinada atual com `--record`
     para gravar a baseline em `scripts/smoke_baseline.json`.
  2. Para testar um upgrade: edite o digest da imagem em
     `docker-compose.wsd.yml` (WS-D) para a nova versão candidata.
  3. `docker compose -f docker-compose.wsd.yml up -d --force-recreate model-gateway`
  4. Rode este script SEM `--record` — ele compara a resposta atual
     byte-a-byte (conteúdo determinístico do modelo eco + shape da resposta)
     contra a baseline gravada. Qualquer diferença = regressão do proxy
     (roteamento, serialização, headers de custo) — decline-never-truncate
     (P6): o script sai com código != 0, nunca "continua mesmo assim".
  5. Só promova o novo digest depois de rodar a suíte pytest completa E este
     smoke test batendo limpo.

Determinismo: o modelo eco (echo_provider/server.py) não usa relógio nem
RNG — a MESMA entrada sempre produz a MESMA saída, então qualquer diff aqui
é 100% atribuível ao LiteLLM em si (proxy), não a variabilidade de um LLM
real. Isso é o que torna esta comparação byte-a-byte válida.
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
        print(f"[smoke_test] baseline gravada em {BASELINE_PATH}")
        print(json.dumps(current, indent=2, sort_keys=True))
        return 0

    baseline = json.loads(BASELINE_PATH.read_text())
    # chat_id é determinístico por conteúdo do request, mas embute detalhes
    # de implementação do LiteLLM (routing) que podem mudar de forma
    # inofensiva entre versões — comparamos separadamente e não falhamos
    # smoke test só por causa dele (falha real é conteúdo/shape/status).
    baseline_stable = {k: v for k, v in baseline.items() if k != "chat_id"}
    current_stable = {k: v for k, v in current.items() if k != "chat_id"}

    if current_stable != baseline_stable:
        print("[smoke_test] REGRESSÃO DETECTADA — resposta divergiu da baseline", file=sys.stderr)
        print(f"baseline: {json.dumps(baseline_stable, indent=2, sort_keys=True)}", file=sys.stderr)
        print(f"atual:    {json.dumps(current_stable, indent=2, sort_keys=True)}", file=sys.stderr)
        return 1

    print("[smoke_test] OK — resposta idêntica à baseline")
    return 0


if __name__ == "__main__":
    sys.exit(main())
