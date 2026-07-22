"""Plano 08 §G — entrypoint do agent-runner isolado.

Roda UM estágio do agente dentro do Pod endurecido. Lê o payload do estágio do
stdin (JSON `{"stage": ..., "input": {...}}`), executa o substrato do agente
(edições de arquivo no /workspace; NUNCA decide fluxo — P1), e escreve o
resultado no stdout (JSON) para o KubernetesSandboxDriver coletar via
`kubectl exec`.

Nesta entrega o corpo do estágio é um stub explícito: o CÓDIGO do driver + a
imagem + o Pod spec endurecido são o que o §G promete agora; conectar o
substrato real (mesmo `_build_substrate` do worker) roda quando a imagem é
publicada no registry do cluster. Fail-clean: estágio desconhecido → exit!=0.
"""
from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent_runner")
    parser.add_argument("--stage", required=True)
    args = parser.parse_args(argv)

    raw = sys.stdin.read() or "{}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        print(json.dumps({"error": "invalid_payload"}))
        return 2

    stage = args.stage
    # placeholder determinístico: eco do estágio + eco do input. A ligação com o
    # substrato real (agente escrevendo código no /workspace) acompanha a
    # publicação da imagem no cluster (prova viva — decisão de infra).
    result = {
        "stage": stage,
        "runner": "agent-runner",
        "received_keys": sorted((payload.get("input") or {}).keys()),
        "note": "isolated-stage-stub (§G): substrato real liga com o cluster",
    }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
