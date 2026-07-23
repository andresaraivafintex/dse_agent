"""Contrato tipado de execução isolada de turno de agente (plano 09, Fase 1).

Invariante 2 da REMEDIATION-CANONICAL-SPEC: "Agent SDK code and untrusted
tools execute only in a stage-scoped sandbox. The Temporal worker dispatches a
typed execution contract; it does not call the agent SDK in its own process."

Este módulo É esse contrato: o worker serializa um `AgentTurnRequest`, o
driver de sandbox o entrega ao agent-runner DENTRO do container/pod
(`docker exec -i` no dev, `kubectl exec -i` no cluster), e o runner devolve um
`AgentTurnResult`. Regras:

  - `extra="forbid"` nos dois lados: payload desconhecido é falha limpa (P6),
    nunca campo ignorado silenciosamente.
  - NENHUM path do host atravessa a fronteira: `workspace_dir` é o caminho
    DENTRO do sandbox (por convenção `/workspace`, bind/emptyDir do driver).
  - NENHUMA credencial de longo prazo: `gateway.virtual_key` é a key efêmera
    por tarefa mintada pelo WS-D; o runner nunca vê master key/provider key.
  - Evolução de shape é ADITIVA (spec §5): campo novo tem default seguro e
    `schema_version` permite recusar payload de versão futura com erro limpo.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

AGENT_TURN_SCHEMA_VERSION = 1

# Substratos que o runner conhece. "fake" existe para conformidade/testes —
# o runtime_profile continua recusando fake em produção (require_real_substrate).
KNOWN_SUBSTRATES = ("fake", "claude-agent", "openhands")


class AgentTurnGateway(BaseModel, extra="forbid"):
    """Triângulo gateway-only (P1): base_url do model-gateway ALCANÇÁVEL DE
    DENTRO do sandbox (rede interna), virtual key efêmera e os headers
    obrigatórios do contrato WS-D (`GatewayCallHeaders.to_http_headers()`)."""

    base_url: str
    virtual_key: str
    headers: dict[str, str] = Field(default_factory=dict)


class AgentTurnRequest(BaseModel, extra="forbid"):
    schema_version: int = AGENT_TURN_SCHEMA_VERSION
    work_item_id: str
    tenant_id: str
    stage: str  # "coder" | "tester" (vocabulário de Stage do gateway_contract)
    substrate: str  # um de KNOWN_SUBSTRATES
    instruction: str
    model: str | None = None
    # Toolset SÓ de edição de arquivo (P1) — o default espelha
    # ClaudeAgentSubstrate.DEFAULT_ALLOWED_TOOLS; git/PR/bash nunca entram.
    allowed_tools: list[str] = Field(
        default_factory=lambda: ["Read", "Write", "Edit", "Glob", "Grep"]
    )
    # Caminho DENTRO do sandbox (convenção do driver), nunca path do host.
    workspace_dir: str = "/workspace"
    timeout_seconds: float = 900.0
    gateway: AgentTurnGateway
    # SÓ para substrate="fake" (conformidade/testes): roteiro do FakeSubstrate.
    fake_script: list[dict] | None = None


class WorkspaceBootstrapRequest(BaseModel, extra="forbid"):
    """Op de lifecycle executada DENTRO do sandbox (runner `--op bootstrap`):
    materializa o workspace git da tarefa no runtime alvo. No Docker o
    checkpoint é o bind mount `/checkpoint.git`; no K8s, o volume do Pod.
    O hook pre-receive de escopo (branch único, sem force-push) é instalado
    no bare repo ANTES do primeiro push — o enforcement mora no remoto."""

    schema_version: int = AGENT_TURN_SCHEMA_VERSION
    work_item_id: str
    branch: str
    base_branch: str = "main"
    workspace_dir: str = "/workspace"
    checkpoint_path: str = "/checkpoint.git"
    provision_checkpoint: bool = True


class WorkspaceBootstrapResult(BaseModel, extra="forbid"):
    schema_version: int = AGENT_TURN_SCHEMA_VERSION
    sha: str = ""
    created: bool = False  # False = workspace/branch já existia (idempotente)
    error: str | None = None
    error_kind: str | None = None

    @property
    def failed(self) -> bool:
        return self.error_kind is not None


class CheckpointOpRequest(BaseModel, extra="forbid"):
    """Op `--op checkpoint`: commit (se houver mudanças) + push do branch da
    tarefa para o checkpoint — refspec fixo, jamais force."""

    schema_version: int = AGENT_TURN_SCHEMA_VERSION
    work_item_id: str
    branch: str
    phase: str
    workspace_dir: str = "/workspace"


class CheckpointOpResult(BaseModel, extra="forbid"):
    schema_version: int = AGENT_TURN_SCHEMA_VERSION
    sha: str = ""
    phase: str = ""
    error: str | None = None
    error_kind: str | None = None

    @property
    def failed(self) -> bool:
        return self.error_kind is not None


class AgentTurnResult(BaseModel, extra="forbid"):
    schema_version: int = AGENT_TURN_SCHEMA_VERSION
    done: bool
    # Diagnóstico compacto do turno (nunca é decisão de fluxo — P1): amostra
    # de pensamentos/tool calls para audit/debug, capada pelo runner.
    thoughts: list[str] = Field(default_factory=list)
    tool_calls: list[str] = Field(default_factory=list)
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    # Falha limpa do runner (P6): erro estruturado, nunca stdout truncado.
    # error_kind é vocabulário fechado para o worker classificar sem substring:
    # "unsupported_substrate" | "invalid_payload" | "substrate_error" | "timeout"
    error: str | None = None
    error_kind: str | None = None

    @property
    def failed(self) -> bool:
        return self.error_kind is not None
