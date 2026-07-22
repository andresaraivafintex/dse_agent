"""Plano 08 §F (F2) — pre-call hook server-side: núcleo evaluate_gate.

Contra o Postgres real (como as demais suítes): kill-switch e budget exaurido
bloqueiam; sem contexto DSE ou dentro do budget, passa. É a enforcement
não-bypassável que espelha o enforce_call do client.
"""
from __future__ import annotations

import os
import uuid

import psycopg2
import pytest

DSN = os.environ.get("DSE_DATABASE_URL", "postgresql://dse:dse_dev_only@localhost:5432/dse")

from dse_budget_hook import evaluate_gate  # noqa: E402


@pytest.fixture
def ids():
    suffix = uuid.uuid4().hex[:10]
    tid, wid = f"tenant-hook-{suffix}", f"wi-hook-{suffix}"
    yield tid, wid
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM gateway_kill_switches WHERE scope_id IN (%s,%s)", (tid, wid))
        cur.execute("DELETE FROM work_item_budgets WHERE work_item_id=%s", (wid,))
        cur.execute("DELETE FROM model_call_ledger WHERE work_item_id=%s", (wid,))
    conn.commit()
    conn.close()


def test_no_dse_context_is_allowed():
    allowed, error, reason = evaluate_gate(None, None)
    assert allowed and reason == "no_dse_context"


def test_kill_switch_blocks(ids):
    tid, wid = ids
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO gateway_kill_switches (scope_type, scope_id, enabled, reason, actor) "
            "VALUES ('work_item', %s, true, 'freio de teste', 'test')",
            (wid,),
        )
    conn.commit()
    conn.close()
    allowed, error, reason = evaluate_gate(tid, wid)
    assert not allowed and error == "kill_switch_active"


def test_budget_exhausted_blocks(ids):
    tid, wid = ids
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO work_item_budgets (work_item_id, tenant_id, max_budget_usd) VALUES (%s,%s,0.01)",
            (wid, tid),
        )
        cur.execute(
            "INSERT INTO model_call_ledger (tenant_id, work_item_id, stage, task_class, model, cost_usd, tokens_in, tokens_out) "
            "VALUES (%s,%s,'coder','default','anthropic/claude',1.00,100,50)",
            (tid, wid),
        )
    conn.commit()
    conn.close()
    allowed, error, reason = evaluate_gate(tid, wid)
    assert not allowed and error == "budget_exhausted"


def test_within_budget_and_no_kill_is_allowed(ids):
    tid, wid = ids
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO work_item_budgets (work_item_id, tenant_id, max_budget_usd) VALUES (%s,%s,10.00)",
            (wid, tid),
        )
        cur.execute(
            "INSERT INTO model_call_ledger (tenant_id, work_item_id, stage, task_class, model, cost_usd, tokens_in, tokens_out) "
            "VALUES (%s,%s,'coder','default','anthropic/claude',0.50,100,50)",
            (tid, wid),
        )
    conn.commit()
    conn.close()
    allowed, error, reason = evaluate_gate(tid, wid)
    assert allowed and reason == "ok"
