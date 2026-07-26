"""WSC-E3-T6: substrate CONFORMANCE suite.

The same tests running against `OpenHandsSubstrate` AND `ClaudeAgentSubstrate`
(parametrize) — the insurance against upstream volatility the plan requires
(risk 5, §18). What conformance proves, per adapter, with BOTH REAL SDKs
installed in this venv (`openhands-sdk` v1.21.0, `claude-agent-sdk` v0.2.124 —
both really `pip install`ed in this session):

  1. structural conformance with the `AgentSubstrate` Protocol;
  2. gateway-only: the constructed session points base_url at the model-gateway
     and its credential = the per-task virtual key; NO provider endpoint
     (anthropic/openai/bedrock) anywhere in the config;
  3. the contract's mandatory headers (`GatewayCallHeaders`) reach the session
     config — they are what WS-D applies policy/budget on at call time
     (per tenant/task/stage caps);
  4. a tool surface with no git/PR (P1: commit/push is deterministic in the
     Activity, never the substrate's);
  5. lifecycle: `run_turn` before `create_session` is a clean error;
  6. swapping substrates is PER-DEPLOYMENT CONFIG (`DSE_CODER_SUBSTRATE`) —
     the factory resolves the adapter with no workflow code change at all; an
     unknown name is a clean failure (P6), never a silent fallback.

What conformance does NOT cover (documented, not hidden): a full turn with real
inference — that requires the model-gateway serving a valid virtual key against
a real provider; the same limitation declared in Phase 1/2 for OpenHands (see the
README, "What is missing for production").
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
        pytest.importorskip("openhands.sdk", reason="openhands-sdk not installed in this venv")
    if name == "claude-agent":
        pytest.importorskip("claude_agent_sdk", reason="claude-agent-sdk not installed in this venv")


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
    """Extracts (base_url, api_key, contract headers) from the REAL config the
    adapter built in the corresponding SDK — per-adapter introspection, the same
    assertion for all of them."""
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
    raise AssertionError(f"substrate without wiring introspection: {type(sub)}")


SUBSTRATES = ["openhands", "claude-agent"]


# ---------------------------------------------------------------------------
# 1) structural conformance with the Protocol
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", SUBSTRATES + ["fake"])
def test_conforms_to_agent_substrate_protocol(name):
    _requires_sdk(name)
    assert isinstance(substrate_from_env(name), AgentSubstrate)


# ---------------------------------------------------------------------------
# 2) gateway-only + 3) policy/budget headers (caps at call time)
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
        assert fragment not in blob, f"substrate config '{name}' references a provider directly: {fragment}"


@pytest.mark.parametrize("name", SUBSTRATES)
def test_contract_headers_reach_session_config(name):
    _, _, headers = _wiring_of(_build(name))
    expected = HEADERS.to_http_headers()
    for k, v in expected.items():
        assert headers.get(k) == v, f"mandatory contract header missing/wrong in '{name}': {k}"


# ---------------------------------------------------------------------------
# 4) tool surface with no git/PR (P1)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", SUBSTRATES)
def test_substrate_surface_has_no_git_or_pr_capability(name):
    sub = _build(name)
    # Structural: the adapter exposes no git/PR method/tool — commit and push
    # are ALWAYS the Activity's (ScopedGitSession), reinforced by the
    # pre-receive hook and the credential scope (Phase 1 suite).
    for forbidden_attr in ("push", "force_push", "commit", "create_pull_request", "merge"):
        assert not hasattr(sub, forbidden_attr)
    if isinstance(sub, ClaudeAgentSubstrate):
        allowed = {t.lower() for t in sub._options.allowed_tools}
        assert allowed, "the Claude Agent SDK allowed_tools must not be empty (that would mean 'all')"
        for tool in allowed:
            assert "git" not in tool and "bash" not in tool and "pull" not in tool


# ---------------------------------------------------------------------------
# 5) lifecycle: clean error without a session
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", SUBSTRATES)
def test_run_turn_before_create_session_fails_cleanly(name):
    _requires_sdk(name)
    sub = substrate_from_env(name)
    with pytest.raises(RuntimeError):
        sub.run_turn("any instruction")


# ---------------------------------------------------------------------------
# 6) swapping via deployment config, with no workflow code change
# ---------------------------------------------------------------------------
def test_substrate_selection_is_deployment_config(monkeypatch):
    monkeypatch.setenv(SUBSTRATE_ENV_VAR, "fake")
    assert isinstance(substrate_from_env(), FakeSubstrate)

    pytest.importorskip("openhands.sdk", reason="openhands-sdk not installed")
    monkeypatch.setenv(SUBSTRATE_ENV_VAR, "openhands")
    assert isinstance(substrate_from_env(), OpenHandsSubstrate)

    pytest.importorskip("claude_agent_sdk", reason="claude-agent-sdk not installed")
    monkeypatch.setenv(SUBSTRATE_ENV_VAR, "claude-agent")
    assert isinstance(substrate_from_env(), ClaudeAgentSubstrate)


def test_unknown_substrate_name_fails_cleanly_never_falls_back(monkeypatch):
    monkeypatch.setenv(SUBSTRATE_ENV_VAR, "some-nonexistent-substrate")
    with pytest.raises(ValueError):
        substrate_from_env()


def test_activity_factory_reads_deployment_config(monkeypatch):
    """The construction point used by the `run_coder_turn` Activity honors the
    same config — the WS-B workflow does not know the substrate (it calls the
    Activity by name), so the swap is 100% deploy-side."""
    from sandbox_runtime.activities import _build_substrate

    monkeypatch.setenv(SUBSTRATE_ENV_VAR, "fake")
    assert isinstance(_build_substrate(None), FakeSubstrate)

    pytest.importorskip("claude_agent_sdk", reason="claude-agent-sdk not installed")
    monkeypatch.setenv(SUBSTRATE_ENV_VAR, "claude-agent")
    assert isinstance(_build_substrate(None), ClaudeAgentSubstrate)
