"""Fase 3 (plano 09) — budget hook FAIL-CLOSED com degradação limitada.

As quatro células da matriz (DB ok/fora × cache fresco/vencido) + o chaos de
conexão real. Antes: blip de Postgres = fail-open silencioso (kill-switch e
teto de tenant não seguravam a chamada). Agora: DB fora serve o último
veredito BOM dentro do hard TTL (contado e logado) e, sem veredito fresco,
BLOQUEIA a chamada DSE com erro retryable — chamada sem contexto DSE nunca é
bloqueada por indisponibilidade.
"""
from __future__ import annotations

import os
import time
import uuid

import psycopg2
import pytest

DSN = os.environ.get("DSE_DATABASE_URL", "postgresql://dse:dse_dev_only@localhost:5432/dse")

import dse_budget_hook as hook  # noqa: E402
from dse_budget_hook import evaluate_gate  # noqa: E402


def _down():
    raise psycopg2.OperationalError("connection refused (simulado)")


@pytest.fixture
def ids():
    suffix = uuid.uuid4().hex[:10]
    tid, wid = f"tenant-fc-{suffix}", f"wi-fc-{suffix}"
    yield tid, wid
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM gateway_kill_switches WHERE scope_id IN (%s,%s)", (tid, wid))
        cur.execute("DELETE FROM work_item_budgets WHERE work_item_id=%s", (wid,))
    conn.commit()
    conn.close()


def test_db_down_with_fresh_allowed_verdict_serves_cache_visibly(ids):
    tid, wid = ids
    degraded_before = hook.DEGRADED_DECISIONS
    # semeia o cache com um veredito REAL (DB ok, dentro do budget)
    allowed, _, reason = evaluate_gate(tid, wid)
    assert allowed and reason == "ok"

    allowed, error, _ = evaluate_gate(tid, wid, connect=_down)
    assert allowed and error == ""  # degradou para o último veredito bom
    assert hook.DEGRADED_DECISIONS == degraded_before + 1  # visível, nunca silencioso


def test_db_down_with_cached_block_stays_blocked(ids):
    tid, wid = ids
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO gateway_kill_switches (scope_type, scope_id, enabled, reason, actor) "
            "VALUES ('work_item', %s, true, 'freio', 'test')",
            (wid,),
        )
    conn.commit()
    conn.close()
    allowed, error, _ = evaluate_gate(tid, wid)
    assert not allowed and error == "kill_switch_active"

    # DB cai: o freio CONTINUA puxado (veredito de bloqueio também é cacheado)
    allowed, error, _ = evaluate_gate(tid, wid, connect=_down)
    assert not allowed and error == "kill_switch_active"


def test_db_down_without_cache_blocks_fail_closed(ids):
    tid, wid = ids
    blocks_before = hook.UNAVAILABLE_BLOCKS
    allowed, error, reason = evaluate_gate(tid, wid, connect=_down)
    assert not allowed
    assert error == "budget_enforcement_unavailable"
    assert "no fresh verdict" in reason
    assert hook.UNAVAILABLE_BLOCKS == blocks_before + 1


def test_db_down_with_stale_cache_blocks_fail_closed(ids):
    tid, wid = ids
    t0 = time.monotonic()
    allowed, _, _ = evaluate_gate(tid, wid, now=lambda: t0)
    assert allowed

    # relógio avança além do hard TTL com o DB fora → cache vencido não vale
    beyond = t0 + hook.HARD_TTL_S + 1
    allowed, error, _ = evaluate_gate(tid, wid, connect=_down, now=lambda: beyond)
    assert not allowed and error == "budget_enforcement_unavailable"


def test_no_dse_context_never_blocked_by_unavailability():
    allowed, _, reason = evaluate_gate(None, None, connect=_down)
    assert allowed and reason == "no_dse_context"


def test_real_connection_failure_is_bounded_and_fail_closed(ids, monkeypatch):
    """Chaos real de conexão: DSN apontando para porta morta — o psycopg2
    falha DENTRO do connect_timeout (um Postgres inacessível não pendura o
    pre-call do proxy) e a decisão é fail-closed."""
    tid, wid = ids
    monkeypatch.setattr(hook, "_DSN", "postgresql://x:x@127.0.0.1:59999/dse")
    started = time.monotonic()
    allowed, error, _ = evaluate_gate(tid, wid)
    elapsed = time.monotonic() - started
    assert not allowed and error == "budget_enforcement_unavailable"
    assert elapsed < hook.CONNECT_TIMEOUT_S + 3  # limitado, nunca pendurado
