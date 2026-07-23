"""Interface de orquestrador de substrato + adapters (WSC-E3-T1).

`AgentSubstrate` é o contrato que qualquer runtime de agente (OpenHands hoje;
outro substrato amanhã) precisa implementar para ser plugado na Activity
`run_coder_turn`. Importante (P1 — nenhuma decisão de fluxo por LLM): o
substrato SÓ EDITA ARQUIVOS no workspace. Ele nunca recebe uma ferramenta de
git push/PR — quem decide commitar e para onde fazer push é código
determinístico em `activities.py` (via `scoped_git.ScopedGitSession`),
depois que o turno termina.

Toda chamada de modelo do substrato tem que sair via o model-gateway
(`dse_contracts.gateway_contract.GatewayCallHeaders` + a virtual key mintada
por `model_gateway_client.mint_virtual_key`) — nunca um SDK de provider
direto. Isso é reforçado tanto pela wiring do `OpenHandsSubstrate` (LLM
sempre construído com `base_url=<gateway>`) quanto pelo egress-proxy (WS-C
E2), que bloquearia mesmo se alguém tentasse burlar isso.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from dse_contracts import CoderTurnResult, GatewayCallHeaders

from .runtime_profile import validate_runtime_profile


@dataclass
class TurnLog:
    instruction: str
    thoughts: list[str] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)
    done: bool = True


@runtime_checkable
class AgentSubstrate(Protocol):
    """Contrato mínimo que todo runtime de agente Coder precisa cumprir."""

    def create_session(
        self,
        *,
        work_item_id: str,
        workspace_dir: str,
        gateway_headers: GatewayCallHeaders,
        virtual_key: str,
        gateway_base_url: str,
    ) -> None:
        ...

    def run_turn(self, instruction: str) -> TurnLog:
        ...

    def collect_artifacts(self) -> CoderTurnResult:
        ...


class FakeSubstrate:
    """Substrato in-memory determinístico para testes (WSC-E3-T1).

    Não chama nenhum LLM, não faz nenhuma requisição de rede — aplica uma
    lista de edições de arquivo pré-roteirizada (`script`), turno a turno.
    Serve para testar toda a plumbing de `run_coder_turn` (sandbox real +
    git de escopo limitado real) sem depender do model-gateway do WS-D
    estar de pé nem gastar inferência real.
    """

    def __init__(self, script: list[dict[str, Any]]):
        self._script = script
        self._turn_idx = 0
        self.workspace_dir: str | None = None
        self.sandbox_id: str = ""
        self._cost_usd = 0.0
        self._tokens_in = 0
        self._tokens_out = 0
        self._files_changed: set[str] = set()

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
        self.sandbox_id = work_item_id
        # Note: FakeSubstrate deliberadamente NÃO usa virtual_key/gateway_base_url
        # (não faz chamada de modelo nenhuma) — mas guarda para provar em teste
        # que a Activity sempre os fornece, mesmo quando o substrato não os usa.
        self._gateway_headers = gateway_headers
        self._virtual_key = virtual_key
        self._gateway_base_url = gateway_base_url

    def run_turn(self, instruction: str) -> TurnLog:
        if self._turn_idx >= len(self._script):
            return TurnLog(instruction=instruction, done=True)
        step = self._script[self._turn_idx]
        self._turn_idx += 1
        assert self.workspace_dir is not None, "create_session precisa ser chamado antes de run_turn"
        for rel_path, content in step.get("write_files", {}).items():
            p = Path(self.workspace_dir) / rel_path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            self._files_changed.add(rel_path)
        self._cost_usd += float(step.get("cost_usd", 0.01))
        self._tokens_in += int(step.get("tokens_in", 100))
        self._tokens_out += int(step.get("tokens_out", 50))
        return TurnLog(
            instruction=instruction,
            thoughts=[step.get("thought", "")],
            tool_calls=[f"write_file:{p}" for p in step.get("write_files", {})],
            done=step.get("done", True),
        )

    def collect_artifacts(self) -> CoderTurnResult:
        return CoderTurnResult(
            sandbox_id=self.sandbox_id,
            diff_summary=f"{len(self._files_changed)} arquivo(s) alterado(s) por FakeSubstrate",
            files_changed=sorted(self._files_changed),
            cost_usd=round(self._cost_usd, 4),
            tokens_in=self._tokens_in,
            tokens_out=self._tokens_out,
        )


class SubstrateUnavailable(Exception):
    """Levantado quando o SDK do substrato pedido não está instalado no venv
    atual (base para as variantes por adapter)."""


class OpenHandsSubstrateUnavailable(SubstrateUnavailable):
    """Levantado quando `openhands-sdk` não está instalado no venv atual."""


class ClaudeAgentSubstrateUnavailable(SubstrateUnavailable):
    """Levantado quando `claude-agent-sdk` não está instalado no venv atual."""


class OpenHandsSubstrate:
    """Adapter real sobre o OpenHands `software-agent-sdk`
    (pacote PyPI `openhands-sdk`, testado disponível nesta sessão em
    `pip install openhands-sdk` — v1.21.0 no momento em que este código foi
    escrito).

    Wiring de produção (P1: nunca um SDK de provider direto):
      `openhands.sdk.LLM(base_url=<model-gateway>, api_key=<virtual_key>,
       extra_headers=GatewayCallHeaders(...).to_http_headers())`
    — o LLM do OpenHands nunca aponta para um provider (Anthropic/OpenAI/
    Bedrock) diretamente; sempre para o model-gateway do WS-D. Mesmo que o
    código tentasse, o egress-proxy (E2) bloquearia a rota de rede.

    NÃO exercitado por turno completo nesta suíte de testes: rodar
    `Conversation.run()` de verdade dispara inferência real, que exige (a)
    o model-gateway do WS-D de pé e respondendo, e (b) uma virtual key válida
    apontando para um provider real configurado no LiteLLM — nenhum dos dois
    está disponível nesta sessão de desenvolvimento paralelo. O teste
    `test_substrate.py::test_openhands_substrate_wiring` cobre apenas a
    CONSTRUÇÃO (LLM/Agent/Workspace apontando para os lugares certos),
    usando `create_session` que não faz I/O de rede por si só.

    Produção também deveria trocar `LocalWorkspace` (executa ferramentas no
    filesystem local do processo que roda o SDK) por `RemoteWorkspace`
    apontando para um `openhands-agent-server` rodando DENTRO do container
    provisionado por `docker_driver.py` — assim a execução de ferramentas do
    agente (bash, edição de arquivo) acontece de fato dentro do sandbox
    isolado, não no processo do worker Temporal. Ver README.md, seção
    "Limitações conhecidas / o que falta para produção".
    """

    def __init__(self, *, model: str | None = None):
        self._model = model or os.environ.get("DSE_CODER_MODEL", "gateway/coder-default")
        self._llm = None
        self._agent = None
        self._conversation = None
        self.workspace_dir: str | None = None
        self.sandbox_id = ""
        self._turns: list[TurnLog] = []

    def create_session(
        self,
        *,
        work_item_id: str,
        workspace_dir: str,
        gateway_headers: GatewayCallHeaders,
        virtual_key: str,
        gateway_base_url: str,
    ) -> None:
        try:
            import openhands.sdk as sdk
        except ImportError as exc:  # pragma: no cover - exercitado só quando o pacote falta
            raise OpenHandsSubstrateUnavailable(
                "openhands-sdk não instalado neste venv. "
                "`pip install openhands-sdk` (testado funcionando em 2026-07 "
                "nesta sessão, v1.21.0). Enquanto isso, use FakeSubstrate."
            ) from exc

        self.workspace_dir = workspace_dir
        self.sandbox_id = work_item_id
        self._llm = sdk.LLM(
            model=self._model,
            base_url=gateway_base_url,
            api_key=virtual_key,
            extra_headers=gateway_headers.to_http_headers(),
        )
        self._agent = sdk.Agent(llm=self._llm)
        self._conversation = sdk.Conversation(
            agent=self._agent,
            workspace=sdk.LocalWorkspace(working_dir=workspace_dir),
        )
        self._turns = []

    def run_turn(self, instruction: str) -> TurnLog:
        if self._conversation is None:
            raise RuntimeError("create_session precisa ser chamado antes de run_turn")
        self._conversation.send_message(instruction)
        self._conversation.run()
        log = TurnLog(instruction=instruction, done=True)
        self._turns.append(log)
        return log

    def collect_artifacts(self) -> CoderTurnResult:
        stats = None
        if self._conversation is not None:
            stats = getattr(self._conversation, "conversation_stats", None)
        cost_usd = 0.0
        tokens_in = 0
        tokens_out = 0
        if stats is not None:
            cost_usd = float(getattr(stats, "total_cost_usd", 0.0) or 0.0)
            tokens_in = int(getattr(stats, "total_tokens_in", 0) or 0)
            tokens_out = int(getattr(stats, "total_tokens_out", 0) or 0)
        return CoderTurnResult(
            sandbox_id=self.sandbox_id,
            diff_summary=f"{len(self._turns)} turno(s) executado(s) via OpenHandsSubstrate",
            files_changed=[],
            cost_usd=cost_usd,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )


class ClaudeAgentSubstrate:
    """Segundo substrato (WSC-E3-T6): adapter real sobre o Claude Agent SDK
    (pacote PyPI `claude-agent-sdk` — `pip install claude-agent-sdk`
    **funcionou nesta sessão**, v0.2.124; o wheel embute o CLI, sem depender
    de um node global). É o seguro contra volatilidade upstream do OpenHands
    (risco 5 do plano; precedentes MinIO/RabbitMQ 2026): mesma interface
    `AgentSubstrate`, mesma suíte de conformidade
    (`tests/test_substrate_conformance.py`), troca por CONFIG DE DEPLOYMENT
    (`DSE_CODER_SUBSTRATE`, ver `substrate_from_env`) — zero mudança em
    código de workflow.

    Wiring gateway-only (P1 — nunca um SDK/endpoint de provider direto): o
    Claude Agent SDK fala com a API Anthropic via o CLI empacotado, que
    respeita as envs `ANTHROPIC_BASE_URL`/`ANTHROPIC_API_KEY`/
    `ANTHROPIC_CUSTOM_HEADERS`. `create_session` constrói
    `ClaudeAgentOptions(env=...)` com base_url = model-gateway, api_key =
    virtual key mintada por tarefa e os headers obrigatórios do contrato
    (`GatewayCallHeaders`) — exatamente o mesmo triângulo do
    `OpenHandsSubstrate`. Mesmo que o processo tentasse outro endpoint, o
    egress-proxy (E2) bloquearia a rota.

    Toolset (P1): `allowed_tools` restrito a leitura/edição de arquivo
    (`Read`/`Write`/`Edit`/`Glob`/`Grep`) — NENHUMA tool de git/PR/bash; o
    commit/push continua determinístico na Activity (`ScopedGitSession`),
    idêntico ao substrato OpenHands. `setting_sources=["project"]` carrega
    SÓ o `.claude/` do workspace (skills tickadas no console + commitadas no
    repo alvo — ver skill_files.py); settings/skills do usuário do HOST
    continuam fora (sessão hermética quanto ao host).

    NÃO exercitado por turno completo nesta suíte (mesma limitação declarada
    do OpenHands): `run_turn` dispara inferência real, que exige o
    model-gateway com provider válido atendendo a virtual key. Os testes de
    conformidade cobrem construção/wiring/seleção — ver README."""

    # Superfície de tools de edição de arquivo do Claude Code — deliberadamente
    # sem Bash/WebFetch/Task e sem qualquer tool git/PR (P1).
    DEFAULT_ALLOWED_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep"]

    def __init__(self, *, model: str | None = None, allowed_tools: list[str] | None = None):
        self._model = model or os.environ.get("DSE_CODER_MODEL", "gateway/coder-default")
        self._allowed_tools = list(allowed_tools or self.DEFAULT_ALLOWED_TOOLS)
        self._options = None
        self.workspace_dir: str | None = None
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
        try:
            import claude_agent_sdk as sdk
        except ImportError as exc:  # pragma: no cover - exercitado só quando o pacote falta
            raise ClaudeAgentSubstrateUnavailable(
                "claude-agent-sdk não instalado neste venv. "
                "`pip install claude-agent-sdk` (testado funcionando em "
                "2026-07 nesta sessão, v0.2.124). Enquanto isso, use "
                "FakeSubstrate ou OpenHandsSubstrate."
            ) from exc

        self.workspace_dir = workspace_dir
        self.sandbox_id = work_item_id
        custom_headers = "\n".join(f"{k}: {v}" for k, v in gateway_headers.to_http_headers().items())
        self._options = sdk.ClaudeAgentOptions(
            model=self._model,
            cwd=workspace_dir,
            allowed_tools=list(self._allowed_tools),
            permission_mode="acceptEdits",
            # "project" = SÓ o workspace (cwd): carrega as skills tickadas no
            # console e materializadas em .claude/skills/ + as commitadas no
            # repo alvo. Continua hermético quanto ao HOST — "user" (settings/
            # skills do usuário da máquina) segue fora da lista.
            setting_sources=["project"],
            env={
                # Gateway-only: o CLI empacotado respeita estas envs — nunca
                # aponta para api.anthropic.com com credencial de provider.
                "ANTHROPIC_BASE_URL": gateway_base_url,
                "ANTHROPIC_API_KEY": virtual_key,
                "ANTHROPIC_CUSTOM_HEADERS": custom_headers,
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            },
        )
        self._turns = []
        self._cost_usd = 0.0
        self._tokens_in = 0
        self._tokens_out = 0

    async def _run_turn_async(self, instruction: str) -> TurnLog:
        import asyncio as _asyncio

        import claude_agent_sdk as sdk

        # Timeout DURO por turn (achado do disparo real 2026-07-22: o CLI
        # pendurou 45+ min sem progresso — heartbeat da Activity continua
        # batendo enquanto a thread trava, então heartbeat ≠ progresso; o
        # start_to_close de 1h só mataria a activity inteira). TimeoutError
        # vira retry normal do Temporal.
        turn_timeout = float(os.environ.get("DSE_CODER_TURN_TIMEOUT_S", "900"))

        log = TurnLog(instruction=instruction, done=True)

        async def _consume() -> None:
            async for message in sdk.query(prompt=instruction, options=self._options):
                if isinstance(message, sdk.AssistantMessage):
                    for block in message.content:
                        if isinstance(block, sdk.TextBlock):
                            log.thoughts.append(block.text)
                        elif isinstance(block, sdk.ToolUseBlock):
                            log.tool_calls.append(block.name)
                elif isinstance(message, sdk.ResultMessage):
                    self._cost_usd += float(message.total_cost_usd or 0.0)
                    usage = message.usage or {}
                    self._tokens_in += int(usage.get("input_tokens", 0) or 0)
                    self._tokens_out += int(usage.get("output_tokens", 0) or 0)

        await _asyncio.wait_for(_consume(), timeout=turn_timeout)
        return log

    def run_turn(self, instruction: str) -> TurnLog:
        if self._options is None:
            raise RuntimeError("create_session precisa ser chamado antes de run_turn")
        import asyncio
        import concurrent.futures

        coro = self._run_turn_async(instruction)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            log = asyncio.run(coro)
        else:
            # Dentro de um event loop (ex.: Activity async do Temporal): roda
            # o turno num loop próprio em thread dedicado — a interface do
            # substrato é síncrona por contrato (paridade com OpenHands).
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                log = ex.submit(asyncio.run, coro).result()
        self._turns.append(log)
        return log

    def collect_artifacts(self) -> CoderTurnResult:
        return CoderTurnResult(
            sandbox_id=self.sandbox_id,
            diff_summary=f"{len(self._turns)} turno(s) executado(s) via ClaudeAgentSubstrate",
            files_changed=[],
            cost_usd=round(self._cost_usd, 6),
            tokens_in=self._tokens_in,
            tokens_out=self._tokens_out,
        )


# ---------------------------------------------------------------------------
# Seleção de substrato por CONFIG DE DEPLOYMENT (WSC-E3-T6) — troca de
# substrato nunca exige mudança em código de workflow: o worker lê
# `DSE_CODER_SUBSTRATE` no deploy e a Activity constrói via esta factory.
# ---------------------------------------------------------------------------
SUBSTRATE_ENV_VAR = "DSE_CODER_SUBSTRATE"

_SUBSTRATE_NAMES = ("fake", "openhands", "claude-agent")


def substrate_from_env(
    name: str | None = None, *, script: list[dict[str, Any]] | None = None
) -> AgentSubstrate:
    """Constrói o substrato configurado: `fake` (default — nenhum modelo),
    `openhands` ou `claude-agent`. Nome desconhecido é falha LIMPA (P6),
    nunca fallback silencioso para outro substrato."""
    chosen = (name or os.environ.get(SUBSTRATE_ENV_VAR, "fake")).strip().lower()
    # Construção direta do fake continua suportada em dev/test, mas jamais
    # pode ser selecionada pelo deployment de produção.
    validate_runtime_profile(require_real_substrate=True, substrate_name=chosen)
    if chosen == "fake":
        return FakeSubstrate(script or [])
    if chosen == "openhands":
        return OpenHandsSubstrate()
    if chosen == "claude-agent":
        return ClaudeAgentSubstrate()
    raise ValueError(
        f"{SUBSTRATE_ENV_VAR}={chosen!r} não é um substrato conhecido "
        f"(válidos: {', '.join(_SUBSTRATE_NAMES)})"
    )
