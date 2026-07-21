"""WSC-E3-T6: suíte de CONFORMIDADE de substrato.

Os mesmos testes rodando contra `OpenHandsSubstrate` E `ClaudeAgentSubstrate`
(parametrize) — o seguro contra volatilidade upstream que o plano exige
(risco 5, §18). O que a conformidade prova, por adapter, com os DOIS SDKs
REAIS instalados neste venv (`openhands-sdk` v1.21.0, `claude-agent-sdk`
v0.2.124 — ambos `pip install` de verdade nesta sessão):

  1. conformidade estrutural com o Protocol `AgentSubstrate`;
  2. gateway-only: a sessão construída aponta base_url para o model-gateway
     e credencial = virtual key por tarefa; NENHUM endpoint de provider
     (anthropic/openai/bedrock) em lugar nenhum da config;
  3. os headers obrigatórios do contrato (`GatewayCallHeaders`) chegam à
     config da sessão — é neles que o WS-D aplica policy/budget no call time
     (caps por tenant/task/stage);
  4. superfície de tools sem git/PR (P1: commit/push é determinístico na
     Activity, nunca do substrato);
  5. lifecycle: `run_turn` antes de `create_session` é erro limpo;
  6. troca de substrato é CONFIG POR DEPLOYMENT (`DSE_CODER_SUBSTRATE`) —
     a factory resolve o adapter sem nenhuma mudança em código de workflow;
     nome desconhecido é falha limpa (P6), nunca fallback silencioso.

O que a conformidade NÃO cobre (documentado, não escondido): um turno
completo com inferência real — exige o model-gateway atendendo uma virtual
key válida com provider real; mesma limitação declarada da Fase 1/2 para o
OpenHands (ver README, "O que falta para produção").
"""
from __future__ import annotations

import pytest

from dse_contracts import GatewayCallHeaders, Stage
from sandbox_runtime.substrate import (
    SUBSTRATE_ENV_VAR,
    AgentSubstrate,
    ClaudeAgentSubstrate,
    FakeSubstrate,
    OpenHandsSubstrate,
    substrate_from_env,
)

GATEWAY_URL = "http://localhost:4000"
VIRTUAL_KEY = "vk-conformance-not-a-real-secret"
PROVIDER_ENDPOINT_FRAGMENTS = (
    "api.anthropic.com",
    "api.openai.com",
    "bedrock",
    "generativelanguage.googleapis.com",
)

HEADERS = GatewayCallHeaders(
    tenant_id="tenant-conf",
    work_item_id="wi-conf",
    stage=Stage.coder,
    task_class="default",
    data_class="internal",
)


def _requires_sdk(name: str):
    if name == "openhands":
        pytest.importorskip("openhands.sdk", reason="openhands-sdk não instalado neste venv")
    if name == "claude-agent":
        pytest.importorskip("claude_agent_sdk", reason="claude-agent-sdk não instalado neste venv")


def _build(name: str) -> AgentSubstrate:
    _requires_sdk(name)
    sub = substrate_from_env(name)
    sub.create_session(
        work_item_id="wi-conf",
        workspace_dir="/tmp",
        gateway_headers=HEADERS,
        virtual_key=VIRTUAL_KEY,
        gateway_base_url=GATEWAY_URL,
    )
    return sub


def _wiring_of(sub: AgentSubstrate) -> tuple[str, str, dict[str, str]]:
    """Extrai (base_url, api_key, headers do contrato) da config REAL que o
    adapter construiu no SDK correspondente — introspecção por adapter, mesma
    asserção para todos."""
    if isinstance(sub, OpenHandsSubstrate):
        key = sub._llm.api_key
        key = key.get_secret_value() if hasattr(key, "get_secret_value") else str(key)
        return str(sub._llm.base_url), key, dict(sub._llm.extra_headers or {})
    if isinstance(sub, ClaudeAgentSubstrate):
        env = sub._options.env
        headers = {}
        for line in env.get("ANTHROPIC_CUSTOM_HEADERS", "").splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip()] = v.strip()
        return env["ANTHROPIC_BASE_URL"], env["ANTHROPIC_API_KEY"], headers
    raise AssertionError(f"substrato sem introspecção de wiring: {type(sub)}")


SUBSTRATES = ["openhands", "claude-agent"]


# ---------------------------------------------------------------------------
# 1) conformidade estrutural com o Protocol
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", SUBSTRATES + ["fake"])
def test_conforms_to_agent_substrate_protocol(name):
    _requires_sdk(name)
    assert isinstance(substrate_from_env(name), AgentSubstrate)


# ---------------------------------------------------------------------------
# 2) gateway-only + 3) headers de policy/budget (caps no call time)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", SUBSTRATES)
def test_session_wires_base_url_to_model_gateway_only(name):
    base_url, api_key, _ = _wiring_of(_build(name))
    assert base_url == GATEWAY_URL
    assert api_key == VIRTUAL_KEY
    for fragment in PROVIDER_ENDPOINT_FRAGMENTS:
        assert fragment not in base_url


@pytest.mark.parametrize("name", SUBSTRATES)
def test_no_provider_endpoint_anywhere_in_session_config(name):
    sub = _build(name)
    if isinstance(sub, ClaudeAgentSubstrate):
        blob = repr(sub._options.__dict__)
    else:
        blob = repr(
            {
                "base_url": sub._llm.base_url,
                "extra_headers": sub._llm.extra_headers,
                "model": getattr(sub._llm, "model", ""),
            }
        )
    for fragment in PROVIDER_ENDPOINT_FRAGMENTS:
        assert fragment not in blob, f"config do substrato '{name}' referencia provider direto: {fragment}"


@pytest.mark.parametrize("name", SUBSTRATES)
def test_contract_headers_reach_session_config(name):
    _, _, headers = _wiring_of(_build(name))
    expected = HEADERS.to_http_headers()
    for k, v in expected.items():
        assert headers.get(k) == v, f"header obrigatório do contrato ausente/errado no '{name}': {k}"


# ---------------------------------------------------------------------------
# 4) superfície de tools sem git/PR (P1)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", SUBSTRATES)
def test_substrate_surface_has_no_git_or_pr_capability(name):
    sub = _build(name)
    # Estrutural: o adapter não expõe nenhum método/tool de git/PR — commit e
    # push são SEMPRE da Activity (ScopedGitSession), reforçados pelo hook
    # pre-receive e pelo escopo da credencial (suíte da Fase 1).
    for forbidden_attr in ("push", "force_push", "commit", "create_pull_request", "merge"):
        assert not hasattr(sub, forbidden_attr)
    if isinstance(sub, ClaudeAgentSubstrate):
        allowed = {t.lower() for t in sub._options.allowed_tools}
        assert allowed, "allowed_tools do Claude Agent SDK não pode ser vazio (seria 'todas')"
        for tool in allowed:
            assert "git" not in tool and "bash" not in tool and "pull" not in tool


# ---------------------------------------------------------------------------
# 5) lifecycle: erro limpo sem sessão
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", SUBSTRATES)
def test_run_turn_before_create_session_fails_cleanly(name):
    _requires_sdk(name)
    sub = substrate_from_env(name)
    with pytest.raises(RuntimeError):
        sub.run_turn("qualquer instrução")


# ---------------------------------------------------------------------------
# 6) troca por config de deployment, sem mudar código de workflow
# ---------------------------------------------------------------------------
def test_substrate_selection_is_deployment_config(monkeypatch):
    monkeypatch.setenv(SUBSTRATE_ENV_VAR, "fake")
    assert isinstance(substrate_from_env(), FakeSubstrate)

    pytest.importorskip("openhands.sdk", reason="openhands-sdk não instalado")
    monkeypatch.setenv(SUBSTRATE_ENV_VAR, "openhands")
    assert isinstance(substrate_from_env(), OpenHandsSubstrate)

    pytest.importorskip("claude_agent_sdk", reason="claude-agent-sdk não instalado")
    monkeypatch.setenv(SUBSTRATE_ENV_VAR, "claude-agent")
    assert isinstance(substrate_from_env(), ClaudeAgentSubstrate)


def test_unknown_substrate_name_fails_cleanly_never_falls_back(monkeypatch):
    monkeypatch.setenv(SUBSTRATE_ENV_VAR, "algum-substrato-inexistente")
    with pytest.raises(ValueError):
        substrate_from_env()


def test_activity_factory_reads_deployment_config(monkeypatch):
    """O ponto de construção usado pela Activity `run_coder_turn` respeita a
    mesma config — o workflow do WS-B não conhece o substrato (chama a
    Activity por nome), logo a troca é 100% deploy-side."""
    from sandbox_runtime.activities import _build_substrate

    monkeypatch.setenv(SUBSTRATE_ENV_VAR, "fake")
    assert isinstance(_build_substrate(None), FakeSubstrate)

    pytest.importorskip("claude_agent_sdk", reason="claude-agent-sdk não instalado")
    monkeypatch.setenv(SUBSTRATE_ENV_VAR, "claude-agent")
    assert isinstance(_build_substrate(None), ClaudeAgentSubstrate)
