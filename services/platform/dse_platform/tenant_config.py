"""Parâmetros de fairness/budget/kill-switch por tenant (migração reservada
do WS-F, `migrations/0007_wsf.sql` — tabela `tenant_config`).

Consumido pelo orchestrator (WS-B, antes de admitir/continuar trabalho para
um tenant) e pelo model-gateway (WS-D, antes de emitir uma chave virtual) —
ver `infra/ALERTING-RULES.md` regra 1 para o fluxo de exaustão de budget que
usa este módulo.

P1 (deterministic-or-human): a leitura/decisão de bloquear um tenant aqui é
puramente uma comparação numérica em código — nunca um LLM decide se um
tenant está sobre budget.
P8 (evidence over assertion): toda mudança de `kill_switch_enabled` grava
uma linha em audit_log via `dse_audit.emit` (nunca silenciosa).
"""
from __future__ import annotations

import dataclasses
import os
from decimal import Decimal
from typing import Any

import psycopg2
import psycopg2.extras
from dse_audit import emit

_DSN = os.environ.get(
    "DSE_PLATFORM_DATABASE_URL",
    os.environ.get("DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"),
)


def _get_connection():
    return psycopg2.connect(_DSN)


@dataclasses.dataclass(frozen=True)
class TenantConfig:
    tenant_id: str
    monthly_budget_usd: Decimal
    max_concurrent_work_items: int
    kill_switch_enabled: bool
    kill_switch_reason: str | None
    fairness: dict[str, Any]


def get_tenant_config(tenant_id: str, conn=None) -> TenantConfig | None:
    """Retorna a config do tenant, ou None se ainda não foi provisionado
    (caller deve tratar `None` como "usar defaults conservadores" — nunca
    como "sem limite")."""
    owns_conn = conn is None
    if owns_conn:
        conn = _get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                """
                SELECT tenant_id, monthly_budget_usd, max_concurrent_work_items,
                       kill_switch_enabled, kill_switch_reason, fairness
                FROM tenant_config WHERE tenant_id = %s
                """,
                (tenant_id,),
            )
            row = cur.fetchone()
    finally:
        if owns_conn:
            conn.close()

    if row is None:
        return None
    return TenantConfig(
        tenant_id=row["tenant_id"],
        monthly_budget_usd=row["monthly_budget_usd"],
        max_concurrent_work_items=row["max_concurrent_work_items"],
        kill_switch_enabled=row["kill_switch_enabled"],
        kill_switch_reason=row["kill_switch_reason"],
        fairness=row["fairness"],
    )


def upsert_tenant_config(
    tenant_id: str,
    *,
    monthly_budget_usd: Decimal | float | None = None,
    max_concurrent_work_items: int | None = None,
    fairness: dict[str, Any] | None = None,
    conn=None,
) -> TenantConfig:
    """Cria a config do tenant (com os defaults da migração) se não existir,
    ou atualiza os campos passados (não-None) se já existir. Idempotente:
    chamar de novo com os mesmos valores não muda nada."""
    owns_conn = conn is None
    if owns_conn:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tenant_config (tenant_id, monthly_budget_usd, max_concurrent_work_items, fairness)
                VALUES (%s, COALESCE(%s, 100.00), COALESCE(%s, 5), COALESCE(%s::jsonb, '{}'::jsonb))
                ON CONFLICT (tenant_id) DO UPDATE SET
                    monthly_budget_usd = COALESCE(EXCLUDED.monthly_budget_usd, tenant_config.monthly_budget_usd),
                    max_concurrent_work_items = COALESCE(EXCLUDED.max_concurrent_work_items, tenant_config.max_concurrent_work_items),
                    fairness = COALESCE(EXCLUDED.fairness, tenant_config.fairness)
                """,
                (
                    tenant_id,
                    monthly_budget_usd,
                    max_concurrent_work_items,
                    None if fairness is None else psycopg2.extras.Json(fairness),
                ),
            )
        if owns_conn:
            conn.commit()
    except Exception:
        if owns_conn:
            conn.rollback()
        raise
    finally:
        if owns_conn:
            conn.close()

    result = get_tenant_config(tenant_id)
    assert result is not None
    return result


def set_kill_switch(
    tenant_id: str,
    *,
    enabled: bool,
    reason: str | None,
    actor: str,
    conn=None,
) -> TenantConfig:
    """Liga/desliga o kill-switch de um tenant. Sempre grava uma linha em
    audit_log (P8) — mesma transação da mudança de estado, para que a
    reconstrução por auditoria (`dse_audit.queries.reconstruct_work_item_history`
    não se aplica aqui, mas o princípio de atomicidade é o mesmo do resto do
    sistema) nunca veja um kill-switch mudado sem o evento correspondente.

    `actor` deve ser um principal resolvido ou `system:<component>` — nunca
    um platform_user_id bruto (mesma regra de `dse_audit.emit`).
    """
    if enabled and not reason:
        raise ValueError("kill_switch_reason é obrigatório ao ATIVAR o kill-switch (P8: nunca silencioso)")

    owns_conn = conn is None
    if owns_conn:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tenant_config (tenant_id, kill_switch_enabled, kill_switch_reason)
                VALUES (%s, %s, %s)
                ON CONFLICT (tenant_id) DO UPDATE SET
                    kill_switch_enabled = EXCLUDED.kill_switch_enabled,
                    kill_switch_reason = EXCLUDED.kill_switch_reason
                """,
                (tenant_id, enabled, reason),
            )
        emit(
            actor=actor,
            action="kill_switch_enabled" if enabled else "kill_switch_disabled",
            tenant_id=tenant_id,
            details={"reason": reason},
            conn=conn,
        )
        if owns_conn:
            conn.commit()
    except Exception:
        if owns_conn:
            conn.rollback()
        raise
    finally:
        if owns_conn:
            conn.close()

    result = get_tenant_config(tenant_id)
    assert result is not None
    return result
