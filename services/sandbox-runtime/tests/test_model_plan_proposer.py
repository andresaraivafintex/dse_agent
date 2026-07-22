"""Planner real (proposer via modelo pelo gateway) — achado do 1º disparo real:
o Planner SEMPRE usava o fixture (expected_files vazio) e toda tarefa escalava
no gate anti-PR-oco. Agora, com substrato real configurado, o modelo propõe o
plano; qualquer falha (import/chamada/JSON) cai no fixture → escala LIMPA (P6).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from sandbox_runtime.activities import _default_plan_proposer, _model_plan_proposer


class _Ctx:
    def render(self) -> str:
        return "## Tarefa\nCorrigir DELETE /api/transactions/:id que apaga a transação errada."


def _inp():
    return SimpleNamespace(instruction="fix delete", work_item_id="wi-x", tenant_id="t")


def _patch_chat(monkeypatch, content: str):
    def fake_chat_completion(**kwargs):
        return SimpleNamespace(content=content, model="anthropic/claude",
                               cost_usd=0.01, tokens_in=100, tokens_out=50, raw={})
    import model_gateway_client.gateway_call as gc
    monkeypatch.setattr(gc, "chat_completion", fake_chat_completion)


def test_model_proposal_parsed_with_files(monkeypatch):
    _patch_chat(monkeypatch, json.dumps({
        "steps": ["localizar o handler de DELETE", "corrigir o lookup por id"],
        "expected_files": ["server/routes/transactions.js", "server/db.js"],
        "test_plan": "curl DELETE ids 2/12/11 e verificar exatidão",
    }))
    p = _model_plan_proposer(_Ctx(), _inp(), headers=None, virtual_key="vk")
    assert p is not None
    assert p["expected_files"] == ["server/routes/transactions.js", "server/db.js"]
    assert len(p["steps"]) == 2


def test_model_proposal_with_markdown_fence(monkeypatch):
    _patch_chat(monkeypatch, "```json\n" + json.dumps({
        "steps": ["s1"], "expected_files": ["a.py"], "test_plan": "t"}) + "\n```")
    p = _model_plan_proposer(_Ctx(), _inp(), headers=None, virtual_key="vk")
    assert p is not None and p["expected_files"] == ["a.py"]


def test_unparseable_response_falls_back_to_none(monkeypatch):
    _patch_chat(monkeypatch, "desculpe, não consigo")
    assert _model_plan_proposer(_Ctx(), _inp(), headers=None, virtual_key="vk") is None


def test_empty_expected_files_from_model_is_rejected(monkeypatch):
    # modelo respondendo JSON com lista vazia → None → fixture → gate escala
    _patch_chat(monkeypatch, json.dumps({"steps": ["s"], "expected_files": [], "test_plan": "t"}))
    assert _model_plan_proposer(_Ctx(), _inp(), headers=None, virtual_key="vk") is None


def test_gateway_error_falls_back_to_none(monkeypatch):
    import model_gateway_client.gateway_call as gc
    def boom(**kwargs):
        raise RuntimeError("budget_exhausted")
    monkeypatch.setattr(gc, "chat_completion", boom)
    assert _model_plan_proposer(_Ctx(), _inp(), headers=None, virtual_key="vk") is None


def test_fixture_still_yields_empty_files():
    p = _default_plan_proposer(_Ctx(), _inp())
    assert p["expected_files"] == []  # e o gate do WS-B escala — deliberado
