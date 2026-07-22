"""L2 REAL (ModelL2ReviewSession) — 3º fixture eliminado no disparo real:
o import do builder apontava para módulo inexistente e todo deployment caía
no fake que aprova sempre. Unit com gateway falso (sem rede)."""
from __future__ import annotations

import json
from types import SimpleNamespace

from dse_contracts.plan_artifact import PlanArtifact
from dse_validation.l2.session import L2ReviewInput, ModelL2ReviewSession, build_l2_session


def _inp():
    return L2ReviewInput(work_item_id="wi", tenant_id="t",
                         plan=PlanArtifact(work_item_id="wi", steps=["s"],
                                           expected_files=["a.js"]),
                         diff="--- a/a.js\n+++ b/a.js\n+fix")


def _patch(monkeypatch, content):
    import model_gateway_client.gateway_call as gc
    import model_gateway_client.virtual_keys as vkm
    monkeypatch.setattr(gc, "chat_completion", lambda **kw: SimpleNamespace(
        content=content, model="m", cost_usd=0.03, tokens_in=1, tokens_out=1, raw={}))
    monkeypatch.setattr(vkm, "mint_virtual_key", lambda *a, **kw: SimpleNamespace(key="vk"))


def test_pass_verdict(monkeypatch):
    _patch(monkeypatch, json.dumps({"passed": True, "objections": []}))
    v = ModelL2ReviewSession().review(_inp())
    assert v.passed and v.objections == [] and v.cost_usd == 0.03


def test_fail_verdict_with_objections(monkeypatch):
    _patch(monkeypatch, "```json\n" + json.dumps(
        {"passed": False, "objections": ["a.js:3 off-by-one"]}) + "\n```")
    v = ModelL2ReviewSession().review(_inp())
    assert not v.passed and v.objections == ["a.js:3 off-by-one"]


def test_builder_selects_real_when_substrate_configured(monkeypatch):
    monkeypatch.setenv("DSE_CODER_SUBSTRATE", "claude-agent")
    assert isinstance(build_l2_session(), ModelL2ReviewSession)
    monkeypatch.setenv("DSE_CODER_SUBSTRATE", "fake")
    from dse_validation.l2.session import FakeL2ReviewSession
    assert isinstance(build_l2_session(), FakeL2ReviewSession)
