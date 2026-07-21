"""Projector (Plano 06 F0) — contrato dos mapas + projeção real no Postgres.

Roda contra o Postgres da infra (como as demais suítes): semeia um WorkItem
sintético + outbox + audit + ledger, roda `run_once` e valida o read model.
Idempotência: segunda passada não duplica nada (replay inofensivo).
"""
from __future__ import annotations

import json
import uuid

import psycopg2
import psycopg2.extras
import pytest

from dse_contracts.work_item import WorkItemStatus

from console_projector.mappers import AUDIT_EVENT_MAP, STATUS_MAP, map_status, split_title
from console_projector.projector import drain, run_once

from conftest import DSN


_SUPER_DSN = DSN.replace("dse_app:dse_app_dev_only", "dse:dse_dev_only")


def _cleanup(wi_id: str) -> None:
    """Teardown com o role de migração (dse_app não deleta ledger — correto,
    o read model herda a disciplina do SoR). Melhor-esforço."""
    try:
        conn = psycopg2.connect(_SUPER_DSN)
    except Exception:
        return
    try:
        with conn.cursor() as cur:
            for stmt in (
                "DELETE FROM console_rm.timeline_events WHERE work_item_id = %s",
                "DELETE FROM console_rm.runs_view WHERE work_item_id = %s",
                "DELETE FROM console_rm.work_items_view WHERE work_item_id = %s",
                "DELETE FROM model_call_ledger WHERE work_item_id = %s",
                "DELETE FROM ingest_events WHERE work_item_id = %s",
                "DELETE FROM work_items WHERE id = %s",
            ):
                cur.execute(stmt, (wi_id,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# contrato dos mapas — muda o enum de um lado, quebra AQUI (não em produção)
# ---------------------------------------------------------------------------

_CONSOLE_STATUSES = {
    "new", "needs_clarification", "ready", "queued", "running", "blocked",
    "pr_ready", "review_feedback", "done", "failed", "cancelled",
}
_CONSOLE_EVENT_TYPES = {
    "created", "status_changed", "comment", "clarification_requested",
    "clarification_answered", "plan", "file_change", "tool_call",
    "test_result", "pr_opened", "feedback", "error", "note",
}


def test_every_fase1_status_has_console_mapping():
    for status in WorkItemStatus:
        mapped, _phase = map_status(status.value)
        assert mapped in _CONSOLE_STATUSES, f"{status.value} -> {mapped} fora do vocabulário"
    # e nada além do enum real vive no mapa (mapa não acumula lixo)
    assert set(STATUS_MAP) == {s.value for s in WorkItemStatus}


def test_audit_event_map_targets_console_event_types():
    assert set(AUDIT_EVENT_MAP.values()) <= _CONSOLE_EVENT_TYPES


def test_split_title():
    t, d = split_title("Account\n\nMake a function that shows balance", fallback="x")
    assert t == "Account" and "balance" in d
    t, d = split_title("", fallback="repo#8")
    assert t == "repo#8" and d == ""


# ---------------------------------------------------------------------------
# projeção real
# ---------------------------------------------------------------------------

def _seed(conn, wi_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO work_items (id, tenant_id, source, source_ref, repo, base_branch,
                                    requester, status, risk_class, budget, idempotency_key)
            VALUES (%s, 'crm-test', 'github', %s::jsonb, 'acme/repo', 'main',
                    'usr_test', 'implementing', 'low', %s::jsonb, %s)
            """,
            (wi_id, json.dumps({"repo": "acme/repo", "number": 42}),
             json.dumps({"max_usd": 5.0}), f"idem-{wi_id}"),
        )
        cur.execute(
            """
            INSERT INTO ingest_events (work_item_id, event_id, kind, payload, processed)
            VALUES (%s, %s, 'task_request', %s::jsonb, true)
            """,
            (wi_id, f"ev-{wi_id}",
             json.dumps({"content_snapshot": "Titulo da tarefa\n\nCorpo detalhado."})),
        )
        cur.execute(
            "INSERT INTO audit_log (work_item_id, tenant_id, actor, action, details) "
            "VALUES (%s, 'crm-test', 'system:test', 'work_item_admitted', '{}'::jsonb), "
            "       (%s, 'crm-test', 'system:test', 'coder_turn_completed', %s::jsonb)",
            (wi_id, wi_id, json.dumps({"cost_usd": 1.23})),
        )
        cur.execute(
            "INSERT INTO model_call_ledger (tenant_id, work_item_id, stage, task_class, model, "
            "cost_usd, tokens_in, tokens_out) VALUES "
            "('crm-test', %s, 'coder', 'default', 'anthropic/claude', 1.23, 1000, 500)",
            (wi_id,),
        )
    conn.commit()


def test_projects_work_item_timeline_and_runs_idempotently():
    conn = psycopg2.connect(DSN)
    wi_id = f"wi-crm-{uuid.uuid4().hex[:10]}"
    try:
        _seed(conn, wi_id)
        drain(conn)
        drain(conn)  # idempotência: replay não duplica

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM console_rm.work_items_view WHERE work_item_id = %s", (wi_id,)
            )
            wi = cur.fetchone()
            assert wi is not None
            assert wi["status"] == "running" and wi["current_phase"] == "coding"
            assert wi["title"] == "Titulo da tarefa"
            assert wi["source_id"] == "acme/repo#42"
            assert float(wi["budget_usd"]) == 5.0
            # last_event veio do audit mais recente
            assert "coder turn completed" in (wi["last_event"] or "")

            cur.execute(
                "SELECT type, message FROM console_rm.timeline_events "
                "WHERE work_item_id = %s ORDER BY audit_id", (wi_id,)
            )
            events = cur.fetchall()
            assert [e["type"] for e in events] == ["created", "file_change"]
            assert "cost_usd=1.23" in events[1]["message"]

            cur.execute(
                "SELECT count(*) AS n, sum(cost_usd) AS cost FROM console_rm.runs_view "
                "WHERE work_item_id = %s", (wi_id,)
            )
            runs = cur.fetchone()
            # DUAS fontes de custo (ledger + audit coder_turn_completed) e
            # nenhuma duplicata na segunda passada (idempotencia por run_key).
            assert runs["n"] == 2
            assert float(runs["cost"]) == pytest.approx(2.46)
    finally:
        conn.rollback()
        conn.close()
        _cleanup(wi_id)


def test_status_transition_reprojects():
    conn = psycopg2.connect(DSN)
    wi_id = f"wi-crm-{uuid.uuid4().hex[:10]}"
    try:
        _seed(conn, wi_id)
        drain(conn)
        with conn.cursor() as cur:
            cur.execute("UPDATE work_items SET status = 'escalated' WHERE id = %s", (wi_id,))
        conn.commit()
        drain(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, current_phase FROM console_rm.work_items_view WHERE work_item_id = %s",
                (wi_id,),
            )
            status, phase = cur.fetchone()
            assert (status, phase) == ("blocked", "escalated")
    finally:
        conn.rollback()
        conn.close()
        _cleanup(wi_id)
