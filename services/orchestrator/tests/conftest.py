from __future__ import annotations

import json
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

# S3 (Phase 5): the real `post_tracking_comment` Activity tries to POST to
# adapter-github. In tests there is no adapter running and the Docker network
# hostname does not resolve on the host — we point it at a dead local port
# (connection refused = instant failure) so the best-effort path does not pile
# up timeouts.
os.environ.setdefault("DSE_ADAPTER_GITHUB_URL", "http://127.0.0.1:1")
os.environ.setdefault("DSE_ADAPTER_SLACK_URL", "http://127.0.0.1:1")
os.environ.setdefault("DSE_ADAPTER_JIRA_URL", "http://127.0.0.1:1")


def new_work_item_id(prefix: str = "wi") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def insert_work_item(
    work_item_id: str,
    *,
    tenant_id: str = "test-tenant",
    requester: str = "usr_test",
    repo: str = "acme/repo",
    base_branch: str = "main",
    source: str = "github",
    source_ref: dict | None = None,
) -> None:
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO work_items
                    (id, tenant_id, source, source_ref, repo, base_branch, requester, idempotency_key)
                VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (work_item_id, tenant_id, source, json.dumps(source_ref or {}),
                 repo, base_branch, requester, f"idem-{work_item_id}"),
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
    """WSB-E3-T2/T3 — reads the durable projection of the gate (migration 0009)."""
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


def read_evidence_row(work_item_id: str):
    """Phase 3 — reads the durable projection of the evidence pipeline (migration 0014)."""
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT preview_status, preview_url, demo_passed, video_artifact_key, "
                "       trace_artifact_key, visual_baseline_key, refresh_count, "
                "       last_refresh_reason, detail "
                "FROM work_item_evidence WHERE work_item_id = %s",
                (work_item_id,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def read_skill_episodes(work_item_id: str):
    """Phase 4 — reads the skill-learning episodes written by a work item
    (source=clarification, skill_episode table from migration 0019/WS-C)."""
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source, pattern_key, occurrence_n, provenance "
                "FROM skill_episode WHERE work_item_id = %s ORDER BY id",
                (work_item_id,),
            )
            return cur.fetchall()
    finally:
        conn.close()


def set_tenant_config(tenant_id: str, *, max_concurrent_work_items: int = 5,
                      max_concurrent_activities: int | None = None) -> None:
    """Seeds/updates tenant_config (WS-F, migration 0007) for the fairness test
    (WSB-E1-T3). `fairness->>'max_concurrent_activities'` takes precedence over
    max_concurrent_work_items."""
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
    """Real Temporal environment (test server with time-skipping) — not a mock:
    it is an actual Temporal server that fast-forwards timers/wait_condition
    when the workflow is blocked with no pending work (needed to test 24h
    reminders / 3-day escalations in seconds)."""
    env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    try:
        yield env
    finally:
        await env.shutdown()


@pytest.fixture(autouse=True)
def _require_postgres():
    """Fails early with a clear message if the foundation infra is not up,
    instead of a cryptic connection error in the middle of a test."""
    try:
        conn = psycopg2.connect(DSN, connect_timeout=3)
        conn.close()
    except Exception as exc:  # pragma: no cover - only in an environment without infra
        pytest.skip(f"foundation Postgres unavailable at {DSN}: {exc}")


async def wait_for_status(handle, expected, attempts: int = 120, sleep_s: float = 0.25) -> str:
    """Resilient polling of a workflow status (Phase 4, plano 09).

    Two runner pitfalls this helper handles, both seen on the first real CI run
    (2 vCPU):

    1. TIME-SKIPPING starved by aggressive polling. Under
       `WorkflowEnvironment.start_time_skipping`, the clock only ADVANCES when
       there is no pending API call — and `handle.query()` PAUSES the skip while
       it runs. A 50ms poll on a saturated runner (slow query) leaves no idle
       window for the clock to jump, so the workflow timers (24h reminder, 3-day
       escalation) never fire and the test HANGS. The larger interval (250ms)
       guarantees the time-skip window between queries.

    2. TRANSIENT query deadline (continue_as_new or a saturated worker: "query
       deadline exceeded" / "Timeout expired") — re-poll instead of propagating.
       Catches the API's RPCError AND the raw RPCError from the Rust bridge (by
       type name), which is not always a subclass of the former.

    Ceiling ~30s (120x250ms) of workflow time — more than enough with the
    time-skip working; the 180s pytest-timeout is the absolute backstop.
    """
    import asyncio

    from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

    status = None
    for _ in range(attempts):
        try:
            status = await handle.query(WorkItemLifecycleWorkflow.get_status)
        except Exception as exc:  # noqa: BLE001 - only a query timeout/deadline is transient
            name = type(exc).__name__
            msg = str(exc).lower()
            if "rpcerror" in name.lower() or "deadline" in msg or "timeout" in msg:
                await asyncio.sleep(sleep_s)
                continue
            raise
        if status in expected:
            return status
        await asyncio.sleep(sleep_s)
    raise AssertionError(f"status never reached {expected}, last={status!r}")
