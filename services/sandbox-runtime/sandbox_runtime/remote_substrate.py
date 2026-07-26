"""RemoteSubstrate — the Coder turn running INSIDE the sandbox (Fase 1).

Implements the SAME `AgentSubstrate` interface as the in-process substrates, but
`run_turn` never touches the agent SDK: it serializes an `AgentTurnRequest`
(typed dse_contracts contract) and dispatches it through
`SandboxDriver.execute_stage` — which hands it to the agent-runner inside the
container (`docker exec -i`, dev) or the Pod (`kubectl exec -i`, cluster). The
workflow/Activity code does not change shape: switching in-process ↔ isolated is
a deployment choice (`DSE_SANDBOX_INPROCESS`), exactly like switching substrate
(`DSE_CODER_SUBSTRATE`).

What NEVER crosses the boundary: host paths (the request carries `/workspace`,
the path as seen from INSIDE the sandbox), the master key/provider credential
(only the ephemeral virtual key), and flow decisions (the result is diagnostics +
cost; the workflow remains the decider — P1).
"""
from __future__ import annotations

import os

from dse_contracts import (
    AgentTurnGateway,
    AgentTurnRequest,
    AgentTurnResult,
    CheckpointOpRequest,
    CheckpointOpResult,
    CoderTurnResult,
    GatewayCallHeaders,
    PostTurnRequest,
    PostTurnResult,
    Stage,
)

from .driver import SandboxDriver, StageExecutionRequest
from .substrate import TurnLog

# model-gateway URL REACHABLE FROM INSIDE the sandbox (the internal
# dse_sandbox_net network / the cluster Service). The worker's default is
# usually localhost — which, inside the container, is the container itself.
SANDBOX_GATEWAY_URL_ENV = "DSE_SANDBOX_GATEWAY_URL"
TURN_TIMEOUT_ENV = "DSE_CODER_TURN_TIMEOUT_S"

# Workspace path INSIDE the sandbox — a convention shared by both drivers
# (bind mount on Docker, emptyDir on the Pod).
SANDBOX_WORKSPACE_DIR = "/workspace"


class RemoteTurnError(RuntimeError):
    """Structured runner failure (P6). `kind` comes from the closed vocabulary of
    `AgentTurnResult.error_kind` — the caller classifies without substring
    matching."""

    def __init__(self, kind: str, message: str):
        super().__init__(f"[{kind}] {message}")
        self.kind = kind


class RemoteSubstrate:
    def __init__(
        self,
        *,
        driver: SandboxDriver,
        substrate_name: str,
        stage: str = "coder",
        model: str | None = None,
        fake_script: list[dict] | None = None,
    ):
        self._driver = driver
        self._name = substrate_name
        self._stage = stage
        self._model = model or os.environ.get("DSE_CODER_MODEL", "gateway/coder-default")
        self._fake_script = fake_script
        self._headers: GatewayCallHeaders | None = None
        self._virtual_key = ""
        self._gateway_base_url = ""
        self.workspace_dir: str | None = None  # host-side; NEVER goes into the request
        self.sandbox_id = ""
        self._turns: list[TurnLog] = []
        self._cost_usd = 0.0
        self._tokens_in = 0
        self._tokens_out = 0

    def create_session(
        self,
        *,
        work_item_id: str,
        workspace_dir: str,
        gateway_headers: GatewayCallHeaders,
        virtual_key: str,
        gateway_base_url: str,
    ) -> None:
        self.workspace_dir = workspace_dir
        self.sandbox_id = self._driver.sandbox_id_for(work_item_id)
        self._headers = gateway_headers
        self._virtual_key = virtual_key
        self._gateway_base_url = os.environ.get(SANDBOX_GATEWAY_URL_ENV) or gateway_base_url
        self._turns = []
        self._cost_usd = 0.0
        self._tokens_in = 0
        self._tokens_out = 0

    def run_turn(self, instruction: str) -> TurnLog:
        if self._headers is None:
            raise RuntimeError("create_session must be called before run_turn")
        timeout = float(os.environ.get(TURN_TIMEOUT_ENV, "900"))
        request = AgentTurnRequest(
            work_item_id=self._headers.work_item_id,
            tenant_id=self._headers.tenant_id,
            stage=self._stage,
            substrate=self._name,
            instruction=instruction,
            model=self._model,
            workspace_dir=SANDBOX_WORKSPACE_DIR,
            timeout_seconds=timeout,
            gateway=AgentTurnGateway(
                base_url=self._gateway_base_url,
                virtual_key=self._virtual_key,
                headers=self._headers.to_http_headers(),
            ),
            fake_script=self._fake_script,
        )
        exec_result = self._driver.execute_stage(
            StageExecutionRequest(
                sandbox_id=self.sandbox_id,
                work_item_id=self._headers.work_item_id,
                tenant_id=self._headers.tenant_id,
                stage=Stage(self._stage),
                input_payload=request.model_dump(),
                # headroom for the exec overhead on top of the turn timeout
                timeout_seconds=timeout + 60.0,
            )
        )
        result = AgentTurnResult.model_validate(exec_result.output_payload)
        if result.failed:
            raise RemoteTurnError(result.error_kind or "substrate_error", result.error or "")

        self._cost_usd += result.cost_usd
        self._tokens_in += result.tokens_in
        self._tokens_out += result.tokens_out
        log = TurnLog(
            instruction=instruction,
            thoughts=list(result.thoughts),
            tool_calls=list(result.tool_calls),
            done=result.done,
        )
        self._turns.append(log)
        return log

    @property
    def driver(self) -> SandboxDriver:
        return self._driver

    def checkpoint_sha(self, *, branch: str, phase: str) -> str:
        """Current SHA of the branch INSIDE the sandbox (the checkpoint is a
        no-op when there are no changes) — replaces the host's
        `ScopedGitSession.current_sha()` on runtimes where the workspace is not
        visible to the worker."""
        if self._headers is None:
            raise RuntimeError("create_session must be called first")
        out = self._driver.execute_op(
            self.sandbox_id,
            "checkpoint",
            CheckpointOpRequest(
                work_item_id=self._headers.work_item_id,
                branch=branch,
                phase=phase,
            ).model_dump(),
        )
        result = CheckpointOpResult.model_validate(out)
        if result.failed:
            raise RemoteTurnError(result.error_kind or "gitops_error", result.error or "")
        return result.sha

    def run_post_turn(
        self, *, branch: str, expected_files: list[str], turn_start_sha: str, commit_message: str
    ) -> PostTurnResult:
        """Deterministic post-turn (prune/lockfile/revert/commit/push) executed
        INSIDE the sandbox — same sequence the worker runs on Docker."""
        if self._headers is None:
            raise RuntimeError("create_session must be called first")
        out = self._driver.execute_op(
            self.sandbox_id,
            "post_turn",
            PostTurnRequest(
                work_item_id=self._headers.work_item_id,
                branch=branch,
                turn_start_sha=turn_start_sha,
                commit_message=commit_message,
                expected_files=expected_files,
            ).model_dump(),
        )
        result = PostTurnResult.model_validate(out)
        if result.failed:
            raise RemoteTurnError(result.error_kind or "gitops_error", result.error or "")
        return result

    def collect_artifacts(self) -> CoderTurnResult:
        return CoderTurnResult(
            sandbox_id=self.sandbox_id,
            diff_summary=f"{len(self._turns)} turno(s) executado(s) via RemoteSubstrate ({self._name})",
            files_changed=[],  # ScopedGitSession is what computes this (deterministic)
            cost_usd=self._cost_usd,
            tokens_in=self._tokens_in,
            tokens_out=self._tokens_out,
        )
