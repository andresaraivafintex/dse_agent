"""Plano 08 §F (F2) — pre-call hook do LiteLLM proxy: enforcement de
budget + kill-switch SERVER-SIDE (não-bypassável).

O `enforce_call` do `model_gateway_client` roda no CLIENTE — um caller que fale
direto com o proxy (com a master key/virtual key) o burlaria. Este hook roda
DENTRO do proxy LiteLLM (`litellm_settings.callbacks`), então TODA chamada
passa por ele, venha de onde vier. É defesa em profundidade: o client segue
checando (fail-fast/UX), o proxy é a autoridade.

Autossuficiente de propósito (só psycopg2, que a imagem do LiteLLM já traz):
para ser dropado na imagem stock sem instalar o `model_gateway_client`.
ESPELHA a lógica de `controls.is_killed` + `budget.resolve_caps`/ledger — se
uma mudar, mude as duas (documentado).

Contexto (tenant/work_item) vem dos headers que o client já envia:
`X-Dse-Tenant-Id` / `X-Dse-Work-Item-Id`. Sem esses headers a chamada não é do
orquestrador DSE — deixa passar (as virtual keys já escopam por modelo/budget).

Fail-safe: erro de DB NÃO bloqueia a chamada (o gateway não deve cair por um
blip do Postgres; o cap `max_budget` da virtual key é o backstop duro). Um
kill-switch/budget exaurido bloqueia com 4xx limpo (P6) + audit (P8).
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
from typing import Any

logger = logging.getLogger("dse_budget_hook")

_DSN = os.environ.get("DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse")


def _conn():
    import psycopg2  # presente na imagem do LiteLLM (suporte a Postgres)
    return psycopg2.connect(_DSN)


# --- kill-switch (espelha controls.is_killed) --------------------------------

def _kill_reason(tenant_id: str, work_item_id: str) -> str | None:
    try:
        conn = _conn()
    except Exception as exc:  # pragma: no cover — fail-safe
        logger.warning("kill-switch check: sem DB (%s); permitindo", exc)
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT scope_type, scope_id, reason FROM gateway_kill_switches WHERE enabled"
            )
            for scope_type, scope_id, reason in cur.fetchall():
                if scope_type == "global":
                    return reason or "global_kill_switch"
                if scope_type == "tenant" and scope_id == tenant_id:
                    return reason or "tenant_kill_switch"
                if scope_type == "work_item" and scope_id == work_item_id:
                    return reason or "work_item_kill_switch"
            # fontes do WS-F
            cur.execute("SELECT reason FROM dse_kill_switch_global WHERE id='global' AND enabled")
            row = cur.fetchone()
            if row:
                return row[0] or "global_kill_switch"
            cur.execute(
                "SELECT kill_switch_reason FROM tenant_config WHERE tenant_id=%s AND kill_switch_enabled",
                (tenant_id,),
            )
            row = cur.fetchone()
            if row:
                return row[0] or "tenant_kill_switch"
        return None
    except Exception as exc:  # pragma: no cover — tabela ausente/permite
        logger.warning("kill-switch check falhou (%s); permitindo", exc)
        return None
    finally:
        conn.close()


# --- budget (espelha budget.resolve_caps + ledger spent) ---------------------

def _one(cur, sql: str, params: tuple) -> Any:
    cur.execute(sql, params)
    row = cur.fetchone()
    return row[0] if row and row[0] is not None else None


def _budget_exhausted(tenant_id: str, work_item_id: str) -> str | None:
    try:
        conn = _conn()
    except Exception as exc:  # pragma: no cover — fail-safe
        logger.warning("budget check: sem DB (%s); permitindo", exc)
        return None
    try:
        with conn.cursor() as cur:
            bundle = _one(
                cur, "SELECT budgets FROM dse_access_bundle WHERE tenant_id=%s AND channel IS NULL LIMIT 1",
                (tenant_id,),
            ) or {}
            if not isinstance(bundle, dict):
                bundle = {}
            wi_cap = bundle.get("per_task_usd")
            if wi_cap is None:
                wi_cap = _one(cur, "SELECT max_budget_usd FROM work_item_budgets WHERE work_item_id=%s", (work_item_id,))
            t_cap = bundle.get("monthly_usd")
            if t_cap is None:
                t_cap = _one(cur, "SELECT monthly_budget_usd FROM tenant_config WHERE tenant_id=%s", (tenant_id,))

            if wi_cap is not None:
                spent = _one(cur, "SELECT COALESCE(sum(cost_usd),0) FROM model_call_ledger WHERE work_item_id=%s", (work_item_id,)) or 0
                if float(spent) >= float(wi_cap):
                    return f"work_item_budget_exhausted({spent:.4f}/{float(wi_cap):.2f})"
            if t_cap is not None:
                month_start = _dt.datetime.now(_dt.timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                spent = _one(
                    cur,
                    "SELECT COALESCE(sum(cost_usd),0) FROM model_call_ledger WHERE tenant_id=%s AND created_at >= %s",
                    (tenant_id, month_start),
                ) or 0
                if float(spent) >= float(t_cap):
                    return f"tenant_monthly_budget_exhausted({spent:.4f}/{float(t_cap):.2f})"
        return None
    except Exception as exc:  # pragma: no cover — tabela ausente/permite
        logger.warning("budget check falhou (%s); permitindo", exc)
        return None
    finally:
        conn.close()


def evaluate_gate(tenant_id: str | None, work_item_id: str | None) -> tuple[bool, str, str]:
    """Núcleo puro e testável (sem litellm): retorna (allowed, error, reason).
    Sem contexto DSE → allowed (não é chamada do orquestrador)."""
    if not tenant_id or not work_item_id:
        return True, "", "no_dse_context"
    reason = _kill_reason(tenant_id, work_item_id)
    if reason:
        return False, "kill_switch_active", reason
    reason = _budget_exhausted(tenant_id, work_item_id)
    if reason:
        return False, "budget_exhausted", reason
    return True, "", "ok"


# --- CustomLogger wrapper (import de litellm guardado p/ testabilidade) -------

try:
    from litellm.integrations.custom_logger import CustomLogger as _Base
except Exception:  # pragma: no cover — venv de dev/teste sem litellm
    class _Base:  # type: ignore
        pass


class DseBudgetKillSwitchHook(_Base):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):  # noqa: ANN001
        headers = (data.get("proxy_server_request") or {}).get("headers") or {}
        tenant_id = headers.get("x-dse-tenant-id")
        work_item_id = headers.get("x-dse-work-item-id")
        allowed, error, reason = evaluate_gate(tenant_id, work_item_id)
        if allowed:
            return data
        _audit(tenant_id, work_item_id, error, reason)
        from fastapi import HTTPException
        status = 403 if error == "kill_switch_active" else 402
        raise HTTPException(status_code=status, detail={"error": error, "reason": reason,
                                                        "enforced_by": "dse_budget_hook"})


def _audit(tenant_id, work_item_id, error, reason) -> None:  # best-effort (P8)
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO audit_log (work_item_id, tenant_id, actor, action, details) "
                "VALUES (%s,%s,'system:model-gateway-proxy',%s,%s::jsonb)",
                (work_item_id, tenant_id, f"proxy_precall_{error}",
                 __import__("json").dumps({"reason": reason})),
            )
        conn.commit()
        conn.close()
    except Exception:  # pragma: no cover
        pass


proxy_handler_instance = DseBudgetKillSwitchHook()
