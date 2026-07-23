"""RemoteSubstrate — o turno do Coder executando DENTRO do sandbox (Fase 1).

Implementa a MESMA interface `AgentSubstrate` dos substratos in-process, mas
`run_turn` nunca toca o SDK do agente: serializa um `AgentTurnRequest` (contrato
tipado do dse_contracts) e o despacha via `SandboxDriver.execute_stage` — que o
entrega ao agent-runner dentro do container (`docker exec -i`, dev) ou do Pod
(`kubectl exec -i`, cluster). O código de workflow/Activity não muda de forma:
a troca in-process ↔ isolado é seleção de deployment (`DSE_SANDBOX_INPROCESS`),
exatamente como a troca de substrato (`DSE_CODER_SUBSTRATE`).

O que NUNCA atravessa a fronteira: paths do host (o request leva `/workspace`,
o caminho de DENTRO do sandbox), master key/credencial de provider (só a
virtual key efêmera) e decisão de fluxo (o resultado é diagnóstico + custo;
quem decide continua sendo o workflow — P1).
"""
from __future__ import annotations

import os

from dse_contracts import (
    AgentTurnGateway,
    AgentTurnRequest,
    AgentTurnResult,
    CoderTurnResult,
    GatewayCallHeaders,
    Stage,
)

from .driver import SandboxDriver, StageExecutionRequest
from .substrate import TurnLog

# URL do model-gateway ALCANÇÁVEL DE DENTRO do sandbox (rede interna
# dse_sandbox_net / Service do cluster). O default do worker costuma ser
# localhost — que dentro do container é o próprio container.
SANDBOX_GATEWAY_URL_ENV = "DSE_SANDBOX_GATEWAY_URL"
TURN_TIMEOUT_ENV = "DSE_CODER_TURN_TIMEOUT_S"

# Caminho do workspace DENTRO do sandbox — convenção dos dois drivers
# (bind mount no Docker, emptyDir no Pod).
SANDBOX_WORKSPACE_DIR = "/workspace"


class RemoteTurnError(RuntimeError):
    """Falha estruturada do runner (P6). `kind` vem do vocabulário fechado de
    `AgentTurnResult.error_kind` — o chamador classifica sem substring."""

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
        self.workspace_dir: str | None = None  # host-side; NUNCA entra no request
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
            raise RuntimeError("create_session precisa ser chamado antes de run_turn")
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
                # margem para o overhead do exec além do timeout do turno
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

    def collect_artifacts(self) -> CoderTurnResult:
        return CoderTurnResult(
            sandbox_id=self.sandbox_id,
            diff_summary=f"{len(self._turns)} turno(s) executado(s) via RemoteSubstrate ({self._name})",
            files_changed=[],  # quem apura é o ScopedGitSession (determinístico)
            cost_usd=self._cost_usd,
            tokens_in=self._tokens_in,
            tokens_out=self._tokens_out,
        )
