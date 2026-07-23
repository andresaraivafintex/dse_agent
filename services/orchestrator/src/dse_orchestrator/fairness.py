"""WSB-E1-T3 — fairness por tenant, worker-side.

Priority & Fairness nativo do Temporal (fairness_key em tasks) so existe no
server 1.31+; a infra da fundacao esta em 1.29 (ver CONVENTIONS.md, "Nota de
infra"). Ate o server suportar, implementamos fairness no worker: um cap de
CONCORRENCIA de Activity POR TENANT. Um tenant que satura seu proprio cap NAO
consome os slots de outro tenant — logo nao empurra o dispatch dos outros
alem do SLO.

Interface trocavel (`FairnessController`): quando o server ganhar P&F nativo,
troca-se `WorkerSideFairnessController` por `NativeFairnessController` (um
no-op local que delega a decisao ao server via `fairness_key` no
`ActivityOptions`) SEM tocar no workflow nem nas Activities — so na montagem
do Worker. Os caps vivem em `tenant_config` (WS-F, migracao 0007): a coluna
`max_concurrent_work_items` e, com precedencia, `fairness->>'max_concurrent_activities'`.

O gating e aplicado por um `FairnessInterceptor` (interceptor de Activity
inbound do Temporal): antes de executar qualquer Activity de negocio, adquire
um slot do tenant (semaforo dimensionado pelo cap) e o libera ao terminar. O
tenant_id e lido do payload da Activity (dict com chave `tenant_id`), que
todas as Activities de negocio de modelo/sandbox carregam.
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

# Cap default quando o tenant nao tem linha em tenant_config (ou a leitura
# falha) — generoso, mas finito, para nunca virar concorrencia ilimitada.
DEFAULT_TENANT_CAP = int(os.environ.get("DSE_FAIRNESS_DEFAULT_CAP", "8"))

# Activities que NAO devem ser gated por fairness (bookkeeping local barato:
# audit, status, checklist, load) — so as Activities caras de tenant
# (modelo/sandbox) competem por slots.
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
    """Interface trocavel. `acquire` e um async context manager que segura um
    slot do tenant pela duracao da Activity."""

    def acquire(self, tenant_id: str) -> Any:  # returns async context manager
        ...


class WorkerSideFairnessController:
    """Cap de concorrencia por tenant via semaforo asyncio (por-worker). O cap
    e resolvido on-demand por `cap_provider` e cacheado; um semaforo por tenant
    e criado sob demanda. Metricas simples de espera expostas para o teste de
    burst provar o SLO."""

    def __init__(self, cap_provider: CapProvider, *, default_cap: int = DEFAULT_TENANT_CAP):
        self._cap_provider = cap_provider
        self._default_cap = default_cap
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._caps: dict[str, int] = {}
        self._lock = asyncio.Lock()
        # observabilidade/teste: maior espera por tenant e contagem de esperas
        self.max_wait_seconds: dict[str, float] = {}
        self.inflight: dict[str, int] = {}

    async def _sem_for(self, tenant_id: str) -> asyncio.Semaphore:
        async with self._lock:
            sem = self._sems.get(tenant_id)
            if sem is None:
                try:
                    cap = int(self._cap_provider(tenant_id))
                except Exception as exc:  # pragma: no cover - defensivo
                    logger.warning("cap_provider falhou p/ %s (%s); usando default", tenant_id, exc)
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
    """Stub para quando o server suportar P&F nativo (1.31+): NAO faz gating no
    worker — a decisao de fairness passa a ser do server via `fairness_key`
    (tenant_id) nos `ActivityOptions`. Deixado aqui para documentar o caminho
    de migracao: trocar a instancia passada ao Worker por esta e adicionar
    `fairness_key=tenant_id` em `workflow.execute_activity` das Activities de
    tenant. Ver README, secao "Fairness (WSB-E1-T3)"."""

    @asynccontextmanager
    async def acquire(self, tenant_id: str):
        yield  # no-op: server-side decide


def postgres_cap_provider(dsn: str | None = None) -> CapProvider:
    """CapProvider que le `tenant_config` (WS-F). Precedencia:
    `fairness->>'max_concurrent_activities'` > `max_concurrent_work_items`.
    Falha -> default (nunca ilimitado). Chamado fora do sandbox de workflow
    (no interceptor de Activity), entao I/O direto e permitido aqui."""
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
    """Interceptor de Worker que insere o gating de fairness antes de CADA
    Activity de negocio de tenant. Nao toca workflows."""

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
