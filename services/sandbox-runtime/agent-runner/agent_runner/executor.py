"""Executor real do turno de agente DENTRO do sandbox (plano 09, Fase 1).

Este módulo roda no container/pod endurecido — nunca no worker. Ele recebe um
`AgentTurnRequest` (contrato tipado do dse_contracts), executa o substrato
pedido contra o `/workspace` local e devolve um `AgentTurnResult`. Regras:

  - P1: o substrato SÓ edita arquivos. Nenhuma tool de git/PR/bash entra no
    toolset; commit/push continuam determinísticos no worker (ScopedGitSession
    sobre o bind/emptyDir do workspace).
  - Gateway-only: o CLI/SDK fala exclusivamente com o model-gateway via a
    virtual key efêmera do request. Nenhuma credencial de longo prazo existe
    neste processo; mesmo que existisse rota, o egress-proxy/NetworkPolicy
    bloqueia provider direto.
  - P6: toda falha vira `AgentTurnResult` estruturado (error_kind de
    vocabulário fechado), nunca stdout truncado nem exceção crua no exec.

Substratos v1: "fake" (conformidade/testes) e "claude-agent" (o substrato dos
disparos reais). "openhands" devolve unsupported_substrate até o
RemoteWorkspace/agent-server ser empacotado nesta imagem — erro limpo, jamais
fallback silencioso.
"""
from __future__ import annotations

import asyncio
import os

from dse_contracts import AgentTurnRequest, AgentTurnResult

# Caps de diagnóstico: o resultado atravessa exec/JSON — nunca deixar um turno
# tagarela estourar o canal (o audit guarda amostra, não transcript).
_MAX_LOG_ITEMS = 50
_MAX_ITEM_CHARS = 2000


def _capped(items: list[str]) -> list[str]:
    return [i[:_MAX_ITEM_CHARS] for i in items[:_MAX_LOG_ITEMS]]


def _run_fake(req: AgentTurnRequest) -> AgentTurnResult:
    """Replay do roteiro (mesma semântica do FakeSubstrate, colapsada num
    exec: o runner é stateless entre execs, então consome o roteiro inteiro
    e reporta o `done` do último passo)."""
    thoughts: list[str] = []
    done = True
    for step in req.fake_script or []:
        for rel_path, content in (step.get("write_files") or {}).items():
            target = os.path.join(req.workspace_dir, rel_path)
            os.makedirs(os.path.dirname(target) or req.workspace_dir, exist_ok=True)
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(content)
        if step.get("thought"):
            thoughts.append(str(step["thought"]))
        done = bool(step.get("done", True))
    return AgentTurnResult(done=done, thoughts=_capped(thoughts))


def build_claude_gateway_env(req: AgentTurnRequest) -> dict[str, str]:
    """Núcleo puro e testável do wiring gateway-only (espelha o
    ClaudeAgentSubstrate do worker — se um mudar, mude os dois)."""
    custom_headers = "\n".join(f"{k}: {v}" for k, v in req.gateway.headers.items())
    return {
        "ANTHROPIC_BASE_URL": req.gateway.base_url,
        "ANTHROPIC_API_KEY": req.gateway.virtual_key,
        "ANTHROPIC_CUSTOM_HEADERS": custom_headers,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }


def _run_claude_agent(req: AgentTurnRequest) -> AgentTurnResult:
    try:
        import claude_agent_sdk as sdk
    except ImportError:
        return AgentTurnResult(
            done=False,
            error="claude-agent-sdk não está instalado na imagem do agent-runner",
            error_kind="substrate_error",
        )

    options = sdk.ClaudeAgentOptions(
        model=req.model,
        cwd=req.workspace_dir,
        allowed_tools=list(req.allowed_tools),
        permission_mode="acceptEdits",
        # "project" = SÓ o workspace: skills materializadas/commitadas no repo
        # alvo. Dentro do sandbox não existe "user" do host para vazar, mas a
        # lista explícita mantém paridade com o substrato do worker.
        setting_sources=["project"],
        env=build_claude_gateway_env(req),
    )

    thoughts: list[str] = []
    tool_calls: list[str] = []
    totals = {"cost": 0.0, "tin": 0, "tout": 0}

    async def _consume() -> None:
        async for message in sdk.query(prompt=req.instruction, options=options):
            if isinstance(message, sdk.AssistantMessage):
                for block in message.content:
                    if isinstance(block, sdk.TextBlock):
                        thoughts.append(block.text)
                    elif isinstance(block, sdk.ToolUseBlock):
                        tool_calls.append(block.name)
            elif isinstance(message, sdk.ResultMessage):
                totals["cost"] += float(message.total_cost_usd or 0.0)
                usage = message.usage or {}
                totals["tin"] += int(usage.get("input_tokens", 0) or 0)
                totals["tout"] += int(usage.get("output_tokens", 0) or 0)

    try:
        asyncio.run(asyncio.wait_for(_consume(), timeout=req.timeout_seconds))
    except (TimeoutError, asyncio.TimeoutError):
        return AgentTurnResult(
            done=False,
            thoughts=_capped(thoughts),
            tool_calls=_capped(tool_calls),
            cost_usd=totals["cost"],
            tokens_in=totals["tin"],
            tokens_out=totals["tout"],
            error=f"turno excedeu {req.timeout_seconds:.0f}s sem concluir",
            error_kind="timeout",
        )
    except Exception as exc:  # noqa: BLE001 — P6: falha estruturada, nunca crua
        return AgentTurnResult(
            done=False,
            thoughts=_capped(thoughts),
            tool_calls=_capped(tool_calls),
            cost_usd=totals["cost"],
            tokens_in=totals["tin"],
            tokens_out=totals["tout"],
            error=f"{type(exc).__name__}: {str(exc)[:500]}",
            error_kind="substrate_error",
        )

    return AgentTurnResult(
        done=True,
        thoughts=_capped(thoughts),
        tool_calls=_capped(tool_calls),
        cost_usd=totals["cost"],
        tokens_in=totals["tin"],
        tokens_out=totals["tout"],
    )


def _run_openhands(req: AgentTurnRequest) -> AgentTurnResult:
    """OpenHands DENTRO do sandbox: aqui `LocalWorkspace` é o desenho CERTO —
    o SDK inteiro (e a execução de ferramentas dele) já está confinado ao
    container/pod; era rodá-lo no worker que quebrava o modelo de ameaça.
    Wiring gateway-only idêntico ao do worker (base_url + virtual key +
    headers do contrato). Instalação é opt-in da imagem
    (requirements-openhands.txt / build-arg INSTALL_OPENHANDS=1)."""
    try:
        import openhands.sdk as sdk
    except ImportError:
        return AgentTurnResult(
            done=False,
            error=(
                "openhands-sdk não está instalado nesta imagem do agent-runner "
                "(build com INSTALL_OPENHANDS=1)"
            ),
            error_kind="substrate_error",
        )

    def _consume() -> AgentTurnResult:
        llm = sdk.LLM(
            model=req.model,
            base_url=req.gateway.base_url,
            api_key=req.gateway.virtual_key,
            extra_headers=dict(req.gateway.headers),
        )
        agent = sdk.Agent(llm=llm)
        conversation = sdk.Conversation(
            agent=agent,
            workspace=sdk.LocalWorkspace(working_dir=req.workspace_dir),
        )
        conversation.send_message(req.instruction)
        conversation.run()
        stats = getattr(conversation, "conversation_stats", None)
        return AgentTurnResult(
            done=True,
            cost_usd=float(getattr(stats, "total_cost_usd", 0.0) or 0.0),
            tokens_in=int(getattr(stats, "total_tokens_in", 0) or 0),
            tokens_out=int(getattr(stats, "total_tokens_out", 0) or 0),
        )

    # Timeout duro por turno (mesma disciplina do claude-agent): o run é
    # síncrono, então roda numa thread vigiada — estouro vira resultado
    # estruturado e o processo efêmero do exec morre logo em seguida.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_consume)
        try:
            return future.result(timeout=req.timeout_seconds)
        except concurrent.futures.TimeoutError:
            return AgentTurnResult(
                done=False,
                error=f"turno openhands excedeu {req.timeout_seconds:.0f}s",
                error_kind="timeout",
            )
        except Exception as exc:  # noqa: BLE001 — P6
            return AgentTurnResult(
                done=False,
                error=f"{type(exc).__name__}: {str(exc)[:500]}",
                error_kind="substrate_error",
            )


def run_agent_turn(req: AgentTurnRequest) -> AgentTurnResult:
    if req.substrate == "fake":
        try:
            return _run_fake(req)
        except OSError as exc:
            # ex.: tentativa de escrever fora do /workspace num rootfs
            # read-only — o SO nega e a negação vira resultado estruturado.
            return AgentTurnResult(
                done=False,
                error=f"{type(exc).__name__}: {str(exc)[:300]}",
                error_kind="substrate_error",
            )
    if req.substrate == "claude-agent":
        return _run_claude_agent(req)
    if req.substrate == "openhands":
        return _run_openhands(req)
    return AgentTurnResult(
        done=False,
        error=f"substrato desconhecido: {req.substrate!r}",
        error_kind="unsupported_substrate",
    )
