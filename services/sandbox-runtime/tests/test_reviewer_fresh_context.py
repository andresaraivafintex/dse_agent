"""WSC-E3-T5: sessão Reviewer de contexto fresco (constrói a sessão; WS-E
orquestra o loop).

Prova (P3 — nenhum produtor aprova o próprio trabalho, e o Reviewer nunca vê o
histórico do Coder):
  - a Activity `run_l2_review` é uma Activity Temporal com o nome do contrato e
    retorna `L2Verdict`;
  - POR CONSTRUÇÃO o contexto do Reviewer contém SÓ plan + diff — não existe
    campo/parâmetro que carregue transcrição/thoughts/tool-calls do Coder;
  - o veredito reflete aderência ao plano (objeções específicas quando não).
"""
from __future__ import annotations

import asyncio

import pytest
from temporalio.testing import ActivityEnvironment

from dse_contracts import ACTIVITY_RUN_L2_REVIEW, L2Verdict, PlanArtifact
from sandbox_runtime.activities import (
    RunL2ReviewInput,
    _run_l2_review_impl,
    run_l2_review,
)
from sandbox_runtime.sessions import FreshReviewerSession, ReviewerContext

_PLAN = PlanArtifact(
    work_item_id="wi-rev",
    steps=["implementar handler"],
    expected_files=["src/handler.py"],
    diff_budget_lines=100,
    test_plan="testar handler",
    risk_class="low",
)

_CLEAN_DIFF = (
    "diff --git a/src/handler.py b/src/handler.py\n"
    "--- a/src/handler.py\n+++ b/src/handler.py\n"
    "@@ -0,0 +1,2 @@\n+def handler():\n+    return 'ok'\n"
)
_OUT_OF_SCOPE_DIFF = (
    "diff --git a/src/secret_stealer.py b/src/secret_stealer.py\n"
    "--- /dev/null\n+++ b/src/secret_stealer.py\n"
    "@@ -0,0 +1,1 @@\n+import os\n"
)


def test_activity_name_matches_contract():
    assert run_l2_review.__temporal_activity_definition.name == ACTIVITY_RUN_L2_REVIEW


def test_reviewer_context_by_construction_has_only_plan_and_diff():
    """A prova estrutural de P3: os campos do contexto do Reviewer e da entrada
    da Activity são EXATAMENTE {plan, diff} (+ ids/classes) — nenhum canal para
    histórico do Coder."""
    ctx_fields = set(ReviewerContext.__dataclass_fields__.keys())
    assert ctx_fields == {"work_item_id", "plan", "diff"}
    for banned in ("coder_history", "transcript", "turns", "thoughts", "tool_calls", "coder_log", "session_history"):
        assert banned not in ctx_fields

    input_fields = set(RunL2ReviewInput.model_fields.keys())
    assert input_fields == {"work_item_id", "tenant_id", "plan", "diff", "task_class", "data_class"}
    for banned in ("coder_history", "transcript", "turns", "coder_log"):
        assert banned not in input_fields

    # A sessão fresca só expõe read_plan/read_diff — nada de repo/history.
    session = FreshReviewerSession(ReviewerContext(work_item_id="x", plan=_PLAN, diff=_CLEAN_DIFF))
    public = {m for m in dir(session) if not m.startswith("_")}
    assert "read_plan" in public and "read_diff" in public
    assert not ({"read_coder_history", "read_transcript", "repo_map", "search_code"} & public)


def test_reviewer_passes_when_diff_adheres_to_plan():
    verdict = asyncio.run(
        _run_l2_review_impl(RunL2ReviewInput(work_item_id="wi-rev", tenant_id="t", plan=_PLAN, diff=_CLEAN_DIFF))
    )
    assert isinstance(verdict, L2Verdict)
    assert verdict.passed is True
    assert verdict.objections == []


def test_reviewer_objects_when_diff_leaves_blast_radius():
    verdict = asyncio.run(
        _run_l2_review_impl(
            RunL2ReviewInput(work_item_id="wi-rev", tenant_id="t", plan=_PLAN, diff=_OUT_OF_SCOPE_DIFF)
        )
    )
    assert verdict.passed is False
    assert any("blast radius" in o for o in verdict.objections)
    assert any("secret_stealer.py" in o for o in verdict.objections)


def test_reviewer_accepts_custom_model_verdict_with_file_line_objections():
    """O substrato de review (fresco) pode devolver objeções específicas de
    arquivo/linha — o loop do WS-E consome isso."""

    def model_verdict(ctx: ReviewerContext):
        return (False, ["src/handler.py:2 — retorno hardcoded 'ok' viola convenção de erros do AGENTS.md"], 0.02)

    verdict = asyncio.run(
        _run_l2_review_impl(
            RunL2ReviewInput(work_item_id="wi-rev", tenant_id="t", plan=_PLAN, diff=_CLEAN_DIFF),
            verdict_fn=model_verdict,
        )
    )
    assert verdict.passed is False
    assert verdict.objections[0].startswith("src/handler.py:2")
    assert verdict.cost_usd == pytest.approx(0.02)


def test_reviewer_runs_through_real_temporal_activity_environment():
    env = ActivityEnvironment()
    verdict = asyncio.run(
        env.run(run_l2_review, RunL2ReviewInput(work_item_id="wi-rev", tenant_id="t", plan=_PLAN, diff=_CLEAN_DIFF))
    )
    assert isinstance(verdict, L2Verdict)
    assert verdict.passed is True
