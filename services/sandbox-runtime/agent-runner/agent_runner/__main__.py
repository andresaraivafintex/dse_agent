"""Entrypoint do agent-runner isolado (plano 08 §G + plano 09 Fase 1).

Roda UM turno de agente dentro do container/pod endurecido. Lê do stdin o
envelope `{"stage": ..., "input": <AgentTurnRequest>}` (formato que os drivers
enviam via `docker exec -i` / `kubectl exec -i`; o request cru sem envelope
também é aceito), valida com o contrato REAL do dse_contracts (spec §5 — nada
de dict solto), executa o substrato via `executor.run_agent_turn` e escreve um
`AgentTurnResult` JSON no stdout.

Disciplina de saída: se conseguimos produzir um resultado ESTRUTURADO (mesmo
de erro — error_kind preenchido), exit 0 e o worker decide pelo payload.
Exit != 0 fica reservado para falha catastrófica (payload indecodificável),
que o driver traduz em falha limpa da Activity — nunca fallback local.
"""
from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent_runner")
    parser.add_argument("--stage", required=True)
    parser.parse_args(argv)

    raw = sys.stdin.read() or "{}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        print(json.dumps({"error": "invalid_payload", "detail": "stdin não é JSON"}))
        return 2

    from dse_contracts import AgentTurnRequest

    body = payload.get("input", payload) if isinstance(payload, dict) else payload
    try:
        request = AgentTurnRequest.model_validate(body)
    except Exception as exc:  # noqa: BLE001 — ValidationError vira resultado estruturado
        from dse_contracts import AgentTurnResult

        result = AgentTurnResult(
            done=False,
            error=f"payload não obedece AgentTurnRequest: {str(exc)[:500]}",
            error_kind="invalid_payload",
        )
        print(result.model_dump_json())
        return 0

    from .executor import run_agent_turn

    result = run_agent_turn(request)
    print(result.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
