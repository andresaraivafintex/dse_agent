"""Entrypoint of the isolated agent-runner (plano 08 §G + plano 09 Fase 1).

Runs ONE agent turn inside the hardened container/pod. Reads from stdin the
envelope `{"stage": ..., "input": <AgentTurnRequest>}` (the format the drivers
send via `docker exec -i` / `kubectl exec -i`; the bare request without the
envelope is also accepted), validates it against the REAL dse_contracts
contract (spec §5 — no loose dicts), runs the substrate through
`executor.run_agent_turn` and writes an `AgentTurnResult` JSON to stdout.

Exit discipline: if we managed to produce a STRUCTURED result (even a failure
one — error_kind filled in), exit 0 and let the worker decide from the payload.
Exit != 0 is reserved for catastrophic failure (undecodable payload), which the
driver turns into a clean Activity failure — never a local fallback.
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
        print(json.dumps({"error": "invalid_payload", "detail": "stdin is not JSON"}))
        return 2

    body = payload.get("input", payload) if isinstance(payload, dict) else payload

    if args.op == "bootstrap":
        from dse_contracts import WorkspaceBootstrapRequest, WorkspaceBootstrapResult

        from .gitops import bootstrap_workspace

        try:
            request = WorkspaceBootstrapRequest.model_validate(body)
        except Exception as exc:  # noqa: BLE001
            print(WorkspaceBootstrapResult(
                error=f"payload does not conform to WorkspaceBootstrapRequest: {str(exc)[:500]}",
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
                error=f"payload does not conform to PostTurnRequest: {str(exc)[:500]}",
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
                error=f"payload does not conform to CheckpointOpRequest: {str(exc)[:500]}",
                error_kind="invalid_payload",
            ).model_dump_json())
            return 0
        print(checkpoint_workspace(request).model_dump_json())
        return 0

    from dse_contracts import AgentTurnRequest

    try:
        request = AgentTurnRequest.model_validate(body)
    except Exception as exc:  # noqa: BLE001 — ValidationError becomes a structured result
        from dse_contracts import AgentTurnResult

        result = AgentTurnResult(
            done=False,
            error=f"payload does not conform to AgentTurnRequest: {str(exc)[:500]}",
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
