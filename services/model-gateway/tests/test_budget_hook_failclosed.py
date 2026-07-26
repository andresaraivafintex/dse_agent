"""Phase 3 (plano 09) — FAIL-CLOSED budget hook with bounded degradation.

The four cells of the matrix (DB up/down × cache fresh/stale) + the real
connection chaos. Before: a Postgres blip = silent fail-open (kill-switch and
tenant cap did not hold the call back). Now: a DB that is down serves the last
GOOD verdict within the hard TTL (counted and logged) and, with no fresh
verdict, BLOCKS the DSE call with a retryable error — a call with no DSE
context is never blocked by unavailability.
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
    # seeds the cache with a REAL verdict (DB up, within budget)
    allowed, _, reason = evaluate_gate(tid, wid)
    assert allowed and reason == "ok"

    allowed, error, _ = evaluate_gate(tid, wid, connect=_down)
    assert allowed and error == ""  # degraded to the last good verdict
    assert hook.DEGRADED_DECISIONS == degraded_before + 1  # visible, never silent


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

    # DB goes down: the brake STAYS pulled (a blocking verdict is cached too)
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

    # clock moves past the hard TTL with the DB down -> a stale cache is not valid
    beyond = t0 + hook.HARD_TTL_S + 1
    allowed, error, _ = evaluate_gate(tid, wid, connect=_down, now=lambda: beyond)
    assert not allowed and error == "budget_enforcement_unavailable"


def test_no_dse_context_never_blocked_by_unavailability():
    allowed, _, reason = evaluate_gate(None, None, connect=_down)
    assert allowed and reason == "no_dse_context"


def test_real_connection_failure_is_bounded_and_fail_closed(ids, monkeypatch):
    """Real connection chaos: DSN pointing at a dead port — psycopg2 fails
    WITHIN connect_timeout (an unreachable Postgres does not hang the proxy's
    pre-call) and the decision is fail-closed."""
    tid, wid = ids
    monkeypatch.setattr(hook, "_DSN", "postgresql://x:x@127.0.0.1:59999/dse")
    started = time.monotonic()
    allowed, error, _ = evaluate_gate(tid, wid)
    elapsed = time.monotonic() - started
    assert not allowed and error == "budget_enforcement_unavailable"
    assert elapsed < hook.CONNECT_TIMEOUT_S + 3  # bounded, never hung
