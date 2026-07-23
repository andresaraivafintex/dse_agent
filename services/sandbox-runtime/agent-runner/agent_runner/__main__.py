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
    parser.add_argument("--stage", default="coder")
    parser.add_argument(
        "--op", default="turn", choices=("turn", "bootstrap", "checkpoint", "post_turn")
    )
    args = parser.parse_args(argv)

    raw = sys.stdin.read() or "{}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        print(json.dumps({"error": "invalid_payload", "detail": "stdin não é JSON"}))
        return 2

    body = payload.get("input", payload) if isinstance(payload, dict) else payload

    if args.op == "bootstrap":
        from dse_contracts import WorkspaceBootstrapRequest, WorkspaceBootstrapResult

        from .gitops import bootstrap_workspace

        try:
            request = WorkspaceBootstrapRequest.model_validate(body)
        except Exception as exc:  # noqa: BLE001
            print(WorkspaceBootstrapResult(
                error=f"payload não obedece WorkspaceBootstrapRequest: {str(exc)[:500]}",
                error_kind="invalid_payload",
            ).model_dump_json())
            return 0
        print(bootstrap_workspace(request).model_dump_json())
        return 0

    if args.op == "post_turn":
        from dse_contracts import PostTurnRequest, PostTurnResult

        from .postturn import run_post_turn

        try:
            request = PostTurnRequest.model_validate(body)
        except Exception as exc:  # noqa: BLE001
            print(PostTurnResult(
                error=f"payload não obedece PostTurnRequest: {str(exc)[:500]}",
                error_kind="invalid_payload",
            ).model_dump_json())
            return 0
        print(run_post_turn(request).model_dump_json())
        return 0

    if args.op == "checkpoint":
        from dse_contracts import CheckpointOpRequest, CheckpointOpResult

        from .gitops import checkpoint_workspace

        try:
            request = CheckpointOpRequest.model_validate(body)
        except Exception as exc:  # noqa: BLE001
            print(CheckpointOpResult(
                error=f"payload não obedece CheckpointOpRequest: {str(exc)[:500]}",
                error_kind="invalid_payload",
            ).model_dump_json())
            return 0
        print(checkpoint_workspace(request).model_dump_json())
        return 0

    from dse_contracts import AgentTurnRequest

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
