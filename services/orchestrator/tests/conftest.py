from __future__ import annotations

import os
import uuid

import psycopg2
import pytest
import pytest_asyncio
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment

DSN = os.environ.get(
    "DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
)


def new_work_item_id(prefix: str = "wi") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def insert_work_item(
    work_item_id: str,
    *,
    tenant_id: str = "test-tenant",
    requester: str = "usr_test",
    repo: str = "acme/repo",
    base_branch: str = "main",
) -> None:
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO work_items
                    (id, tenant_id, source, source_ref, repo, base_branch, requester, idempotency_key)
                VALUES (%s, %s, 'github', '{}'::jsonb, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (work_item_id, tenant_id, repo, base_branch, requester, f"idem-{work_item_id}"),
            )
        conn.commit()
    finally:
        conn.close()


def read_work_item(work_item_id: str):
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status, pr_number FROM work_items WHERE id = %s", (work_item_id,))
            return cur.fetchone()
    finally:
        conn.close()


def read_audit_actions(work_item_id: str) -> list[str]:
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT action FROM audit_log WHERE work_item_id = %s ORDER BY id", (work_item_id,)
            )
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def read_gate_row(work_item_id: str):
    """WSB-E3-T2/T3 — le a projecao duravel do gate (migracao 0009)."""
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, auto_approved, resolved_approvers, decided_by, "
                "       rejection_route, justification, risk_class, plan_round "
                "FROM plan_approval_gate WHERE work_item_id = %s",
                (work_item_id,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def set_tenant_config(tenant_id: str, *, max_concurrent_work_items: int = 5,
                      max_concurrent_activities: int | None = None) -> None:
    """Semeia/atualiza tenant_config (WS-F, migracao 0007) para o teste de
    fairness (WSB-E1-T3). `fairness->>'max_concurrent_activities'` tem
    precedencia sobre max_concurrent_work_items."""
    import json

    fairness = {}
    if max_concurrent_activities is not None:
        fairness["max_concurrent_activities"] = max_concurrent_activities
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tenant_config (tenant_id, max_concurrent_work_items, fairness)
                VALUES (%s, %s, %s::jsonb)
                ON CONFLICT (tenant_id) DO UPDATE SET
                    max_concurrent_work_items = EXCLUDED.max_concurrent_work_items,
                    fairness = EXCLUDED.fairness
                """,
                (tenant_id, max_concurrent_work_items, json.dumps(fairness)),
            )
        conn.commit()
    finally:
        conn.close()


@pytest_asyncio.fixture
async def time_skipping_env():
    """Ambiente Temporal real (servidor de teste com time-skipping) — nao e
    um mock: e um servidor Temporal de verdade que acelera timers/wait_condition
    quando o workflow esta bloqueado sem trabalho pendente (necessario para
    testar reminders de 24h/escalacao de 3 dias em segundos)."""
    env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    try:
        yield env
    finally:
        await env.shutdown()


@pytest.fixture(autouse=True)
def _require_postgres():
    """Falha cedo e com uma mensagem clara se a infra da fundacao nao
    estiver no ar, em vez de um erro de conexao criptico no meio do teste."""
    try:
        conn = psycopg2.connect(DSN, connect_timeout=3)
        conn.close()
    except Exception as exc:  # pragma: no cover - so em ambiente sem infra
        pytest.skip(f"Postgres da fundacao indisponivel em {DSN}: {exc}")
