"""WSB-E1-T3 — per-tenant fairness, worker-side.

Temporal's native Priority & Fairness (fairness_key on tasks) only exists on
server 1.31+; the foundation's infra is on 1.29 (see CONVENTIONS.md, "Infra
note"). Until the server supports it, we implement fairness on the worker: a
PER-TENANT Activity CONCURRENCY cap. A tenant that saturates its own cap does
NOT consume another tenant's slots — so it does not push the others' dispatch
beyond the SLO.

Swappable interface (`FairnessController`): once the server gains native P&F,
swap `WorkerSideFairnessController` for `NativeFairnessController` (a local
no-op that delegates the decision to the server via `fairness_key` in
`ActivityOptions`) WITHOUT touching the workflow or the Activities — only the
Worker assembly. The caps live in `tenant_config` (WS-F, migration 0007): the
`max_concurrent_work_items` column and, taking precedence,
`fairness->>'max_concurrent_activities'`.

The gating is applied by a `FairnessInterceptor` (Temporal inbound Activity
interceptor): before executing any business Activity, it acquires a tenant slot
(a semaphore sized by the cap) and releases it on completion. The tenant_id is
read from the Activity payload (a dict with a `tenant_id` key), which every
model/sandbox business Activity carries.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Callable, Protocol

from temporalio import activity
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    Interceptor,
)

logger = logging.getLogger("dse_orchestrator.fairness")

# Default cap when the tenant has no row in tenant_config (or the read fails) —
# generous, but finite, so it never becomes unbounded concurrency.
DEFAULT_TENANT_CAP = int(os.environ.get("DSE_FAIRNESS_DEFAULT_CAP", "8"))

# Activities that must NOT be gated by fairness (cheap local bookkeeping: audit,
# status, checklist, load) — only the expensive tenant Activities
# (model/sandbox) compete for slots.
_UNGATED_ACTIVITIES: frozenset[str] = frozenset(
    {
        "emit_audit_event",
        "update_work_item_status",
        "check_clarification_completeness",
        "load_work_item",
        "evaluate_plan_approval_policy",
        "resolve_plan_approver",
        "record_plan_approval",
    }
)


CapProvider = Callable[[str], int]


class FairnessController(Protocol):
    """Swappable interface. `acquire` is an async context manager that holds a
    tenant slot for the duration of the Activity."""

    def acquire(self, tenant_id: str) -> Any:  # returns async context manager
        ...


class WorkerSideFairnessController:
    """Per-tenant concurrency cap via an asyncio semaphore (per worker). The cap
    is resolved on demand by `cap_provider` and cached; one semaphore per tenant
    is created lazily. Simple wait metrics are exposed so the burst test can
    prove the SLO."""

    def __init__(self, cap_provider: CapProvider, *, default_cap: int = DEFAULT_TENANT_CAP):
        self._cap_provider = cap_provider
        self._default_cap = default_cap
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._caps: dict[str, int] = {}
        self._lock = asyncio.Lock()
        # observability/test: longest wait per tenant and in-flight count
        self.max_wait_seconds: dict[str, float] = {}
        self.inflight: dict[str, int] = {}

    async def _sem_for(self, tenant_id: str) -> asyncio.Semaphore:
        async with self._lock:
            sem = self._sems.get(tenant_id)
            if sem is None:
                try:
                    cap = int(self._cap_provider(tenant_id))
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("cap_provider failed for %s (%s); using the default", tenant_id, exc)
                    cap = self._default_cap
                cap = max(1, cap)
                self._caps[tenant_id] = cap
                sem = asyncio.Semaphore(cap)
                self._sems[tenant_id] = sem
            return sem

    @asynccontextmanager
    async def acquire(self, tenant_id: str):
        sem = await self._sem_for(tenant_id)
        started = time.monotonic()
        await sem.acquire()
        waited = time.monotonic() - started
        prev = self.max_wait_seconds.get(tenant_id, 0.0)
        if waited > prev:
            self.max_wait_seconds[tenant_id] = waited
        self.inflight[tenant_id] = self.inflight.get(tenant_id, 0) + 1
        try:
            yield
        finally:
            self.inflight[tenant_id] = self.inflight.get(tenant_id, 1) - 1
            sem.release()


class NativeFairnessController:
    """Stub for when the server supports native P&F (1.31+): it does NO gating
    on the worker — the fairness decision moves to the server via `fairness_key`
    (tenant_id) in `ActivityOptions`. Kept here to document the migration path:
    swap the instance passed to the Worker for this one and add
    `fairness_key=tenant_id` to `workflow.execute_activity` for the tenant
    Activities. See the README, section "Fairness (WSB-E1-T3)"."""

    @asynccontextmanager
    async def acquire(self, tenant_id: str):
        yield  # no-op: the server decides


def postgres_cap_provider(dsn: str | None = None) -> CapProvider:
    """CapProvider that reads `tenant_config` (WS-F). Precedence:
    `fairness->>'max_concurrent_activities'` > `max_concurrent_work_items`.
    Failure -> default (never unlimited). Called outside the workflow sandbox
    (in the Activity interceptor), so direct I/O is allowed here."""
    _dsn = dsn or os.environ.get(
        "DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
    )

    def _provider(tenant_id: str) -> int:
        import psycopg2

        try:
            conn = psycopg2.connect(_dsn, connect_timeout=3)
        except Exception:
            return DEFAULT_TENANT_CAP
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT max_concurrent_work_items, "
                    "       (fairness->>'max_concurrent_activities') "
                    "FROM tenant_config WHERE tenant_id = %s",
                    (tenant_id,),
                )
                row = cur.fetchone()
            if row is None:
                return DEFAULT_TENANT_CAP
            max_wi, max_act = row
            if max_act is not None and str(max_act).strip() != "":
                return int(max_act)
            if max_wi is not None:
                return int(max_wi)
            return DEFAULT_TENANT_CAP
        finally:
            conn.close()

    return _provider


class FairnessInterceptor(Interceptor):
    """Worker interceptor that inserts the fairness gating before EVERY tenant
    business Activity. It does not touch workflows."""

    def __init__(self, controller: FairnessController):
        self._controller = controller

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        return _FairnessActivityInbound(next, self._controller)


class _FairnessActivityInbound(ActivityInboundInterceptor):
    def __init__(self, next: ActivityInboundInterceptor, controller: FairnessController):
        super().__init__(next)
        self._controller = controller

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        info = activity.info()
        act_type = info.activity_type
        tenant_id = _extract_tenant_id(input.args)
        if act_type in _UNGATED_ACTIVITIES or tenant_id is None:
            return await self.next.execute_activity(input)
        async with self._controller.acquire(tenant_id):
            return await self.next.execute_activity(input)


def _extract_tenant_id(args: Any) -> str | None:
    for arg in (args or []):
        if isinstance(arg, dict) and arg.get("tenant_id"):
            return str(arg["tenant_id"])
        tid = getattr(arg, "tenant_id", None)
        if tid:
            return str(tid)
    return None
