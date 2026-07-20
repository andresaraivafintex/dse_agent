"""WSF-E6-T2 — kill switches nos 4 escopos + quarentena de work item.

Escopos (do mais amplo ao mais estreito):
  1. GLOBAL  — `dse_kill_switch_global` (este WS). Bloqueia toda admissão em
               todos os tenants.
  2. TENANT  — `tenant_config.kill_switch_enabled` (dse_platform.tenant_config).
  3. CHANNEL — `channel_kill_switches` (tabela do WS-A; escrevemos linhas nela
               no mesmo banco — data-plane, não editamos o arquivo/migração do
               WS-A). Bloqueia admissão de um (tenant, canal) específico.
  4. TASK    — quarentena durável de um work item (`dse_work_item_quarantine`) +
               (no operator.py) o signal Temporal `pause`/`cancel`. O flag
               durável sobrevive a restart do worker e alimenta a projeção do
               queue board.

`is_admission_blocked(tenant, channel)` é o composto que o ingest-gateway (WS-A)
e o model-gateway (WS-D) devem consultar: checa global -> tenant -> canal, nessa
ordem, e devolve o primeiro bloqueio encontrado (ou None). Toda mudança de
qualquer switch grava audit (P8).
"""
from __future__ import annotations

import dataclasses
import os

import psycopg2
import psycopg2.extras
from dse_audit import emit

from .tenant_config import get_tenant_config, set_kill_switch as _set_tenant_kill_switch

_DSN = os.environ.get(
    "DSE_PLATFORM_DATABASE_URL",
    os.environ.get("DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"),
)


def _get_connection():
    return psycopg2.connect(_DSN)


@dataclasses.dataclass(frozen=True)
class AdmissionBlock:
    scope: str  # "global" | "tenant" | "channel"
    reason: str | None


# ---------------------------------------------------------------------------
# 1. GLOBAL
# ---------------------------------------------------------------------------
def set_global_kill_switch(*, enabled: bool, reason: str | None, actor: str, conn=None) -> None:
    if enabled and not reason:
        raise ValueError("global kill switch exige `reason` ao ATIVAR (P8)")
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO dse_kill_switch_global (id, enabled, reason)
                VALUES ('global', %s, %s)
                ON CONFLICT (id) DO UPDATE SET enabled = EXCLUDED.enabled, reason = EXCLUDED.reason
                """,
                (enabled, reason),
            )
        emit(
            actor=actor,
            action="global_kill_switch_enabled" if enabled else "global_kill_switch_disabled",
            tenant_id="platform",
            details={"reason": reason},
            conn=conn,
        )
        if owns:
            conn.commit()
    except Exception:
        if owns:
            conn.rollback()
        raise
    finally:
        if owns:
            conn.close()


def get_global_kill_switch(conn=None) -> tuple[bool, str | None]:
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT enabled, reason FROM dse_kill_switch_global WHERE id = 'global'")
            row = cur.fetchone()
    finally:
        if owns:
            conn.close()
    if row is None:
        return (False, None)
    return (row[0], row[1])


# ---------------------------------------------------------------------------
# 2. TENANT (delega ao módulo tenant_config, que já audita)
# ---------------------------------------------------------------------------
def set_tenant_kill_switch(tenant_id: str, *, enabled: bool, reason: str | None, actor: str, conn=None):
    return _set_tenant_kill_switch(tenant_id, enabled=enabled, reason=reason, actor=actor, conn=conn)


# ---------------------------------------------------------------------------
# 3. CHANNEL (tabela channel_kill_switches do WS-A — data-plane)
# ---------------------------------------------------------------------------
def set_channel_kill_switch(
    tenant_id: str, channel: str, *, active: bool, reason: str | None, actor: str, conn=None
) -> None:
    """Liga/desliga o kill switch de um (tenant, canal). `active = true` =
    canal DESLIGADO (bloqueia admissão) — mesma semântica da coluna `active`
    da tabela do WS-A."""
    if active and not reason:
        raise ValueError("channel kill switch exige `reason` ao ATIVAR (P8)")
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO channel_kill_switches (tenant_id, channel, active, reason)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (tenant_id, channel) DO UPDATE SET active = EXCLUDED.active, reason = EXCLUDED.reason
                """,
                (tenant_id, channel, active, reason),
            )
        emit(
            actor=actor,
            action="channel_kill_switch_enabled" if active else "channel_kill_switch_disabled",
            tenant_id=tenant_id,
            details={"channel": channel, "reason": reason},
            conn=conn,
        )
        if owns:
            conn.commit()
    except Exception:
        if owns:
            conn.rollback()
        raise
    finally:
        if owns:
            conn.close()


# ---------------------------------------------------------------------------
# Composto de admissão (global -> tenant -> canal)
# ---------------------------------------------------------------------------
def is_admission_blocked(tenant_id: str, channel: str | None = None, conn=None) -> AdmissionBlock | None:
    """Retorna o primeiro bloqueio (mais amplo primeiro) ou None se admissível.
    Deve ser chamado pelo ingest-gateway (WS-A) e pelo model-gateway (WS-D)
    ANTES de admitir/rodar trabalho. Puramente determinístico (P1)."""
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        g_enabled, g_reason = get_global_kill_switch(conn=conn)
        if g_enabled:
            return AdmissionBlock(scope="global", reason=g_reason)

        cfg = get_tenant_config(tenant_id, conn=conn)
        if cfg is not None and cfg.kill_switch_enabled:
            return AdmissionBlock(scope="tenant", reason=cfg.kill_switch_reason)

        if channel is not None:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT active, reason FROM channel_kill_switches WHERE tenant_id = %s AND channel = %s",
                    (tenant_id, channel),
                )
                row = cur.fetchone()
            if row is not None and row[0]:
                return AdmissionBlock(scope="channel", reason=row[1])
        return None
    finally:
        if owns:
            conn.close()


# ---------------------------------------------------------------------------
# 4. TASK — quarentena durável de work item (par do signal em operator.py)
# ---------------------------------------------------------------------------
def quarantine_work_item(
    work_item_id: str, tenant_id: str, *, reason: str, actor: str, conn=None
) -> None:
    """Marca um work item como em quarentena (durável). Idempotente: re-quarantinar
    um já-quarantinado só atualiza a razão. `reason`/`actor` obrigatórios (P8)."""
    if not reason:
        raise ValueError("quarantine exige `reason` (P8)")
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO dse_work_item_quarantine (work_item_id, tenant_id, reason, actor)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (work_item_id) DO UPDATE SET
                    reason = EXCLUDED.reason, actor = EXCLUDED.actor,
                    released_at = NULL, released_by = NULL
                """,
                (work_item_id, tenant_id, reason, actor),
            )
        emit(
            actor=actor, action="work_item_quarantined", tenant_id=tenant_id,
            work_item_id=work_item_id, details={"reason": reason}, conn=conn,
        )
        if owns:
            conn.commit()
    except Exception:
        if owns:
            conn.rollback()
        raise
    finally:
        if owns:
            conn.close()


def release_quarantine(work_item_id: str, tenant_id: str, *, actor: str, conn=None) -> None:
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE dse_work_item_quarantine
                SET released_at = now(), released_by = %s
                WHERE work_item_id = %s AND released_at IS NULL
                """,
                (actor, work_item_id),
            )
        emit(
            actor=actor, action="work_item_quarantine_released", tenant_id=tenant_id,
            work_item_id=work_item_id, details={}, conn=conn,
        )
        if owns:
            conn.commit()
    except Exception:
        if owns:
            conn.rollback()
        raise
    finally:
        if owns:
            conn.close()


def is_quarantined(work_item_id: str, conn=None) -> bool:
    owns = conn is None
    if owns:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM dse_work_item_quarantine WHERE work_item_id = %s AND released_at IS NULL",
                (work_item_id,),
            )
            return cur.fetchone() is not None
    finally:
        if owns:
            conn.close()
