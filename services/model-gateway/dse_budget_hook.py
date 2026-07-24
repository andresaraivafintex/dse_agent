"""Plano 08 §F (F2) + plano 09 Fase 3 — pre-call hook do LiteLLM proxy:
enforcement de budget + kill-switch SERVER-SIDE (não-bypassável) e FAIL-CLOSED
com degradação limitada.

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

Postura em falha de Postgres (Fase 3 — antes era fail-OPEN silencioso):
  1. Toda chamada consulta o DB (com connect_timeout curto — um Postgres
     PENDURADO não trava mais o pre-call) e o veredito bom mais recente por
     (tenant, work_item) fica em cache no processo.
  2. DB inacessível + veredito em cache dentro do HARD TTL → serve o cache,
     LOGADO como decisão DEGRADADA (visível, contável, com prazo de validade).
  3. DB inacessível + cache ausente/vencido → BLOQUEIA a chamada DSE com 503
     retryable ("budget_enforcement_unavailable") — fail-closed de verdade.
     Chamadas SEM contexto DSE nunca são bloqueadas por indisponibilidade
     (as virtual keys já as escopam; o cap max_budget segue de backstop).
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import time
from typing import Any, Callable

logger = logging.getLogger("dse_budget_hook")

_DSN = os.environ.get("DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse")

# Knobs da degradação (Fase 3). CONNECT_TIMEOUT curto é o que impede um
# Postgres pendurado de segurar o event loop do proxy.
CONNECT_TIMEOUT_S = int(os.environ.get("DSE_BUDGET_HOOK_CONNECT_TIMEOUT_S", "3"))
HARD_TTL_S = float(os.environ.get("DSE_BUDGET_HOOK_HARD_TTL_S", "600"))

# Contadores de observabilidade (expostos p/ teste e scrape via logs):
# toda decisão fora do caminho feliz é CONTADA e LOGADA — fail-open silencioso
# não existe mais.
DEGRADED_DECISIONS = 0          # serviu veredito de cache com DB fora
UNAVAILABLE_BLOCKS = 0          # bloqueou por indisponibilidade sem cache

# (tenant_id, work_item_id) -> ((allowed, error, reason), monotonic_ts)
_VERDICT_CACHE: dict[tuple[str, str], tuple[tuple[bool, str, str], float]] = {}


def _conn():
    import psycopg2  # presente na imagem do LiteLLM (suporte a Postgres)
    return psycopg2.connect(_DSN, connect_timeout=CONNECT_TIMEOUT_S)


# --- kill-switch (espelha controls.is_killed) --------------------------------

def _kill_reason(cur, tenant_id: str, work_item_id: str) -> str | None:
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


# --- budget (espelha budget.resolve_caps + ledger spent) ---------------------

def _one(cur, sql: str, params: tuple) -> Any:
    cur.execute(sql, params)
    row = cur.fetchone()
    return row[0] if row and row[0] is not None else None


def _budget_exhausted(cur, tenant_id: str, work_item_id: str) -> str | None:
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


def _decide(cur, tenant_id: str, work_item_id: str) -> tuple[bool, str, str]:
    reason = _kill_reason(cur, tenant_id, work_item_id)
    if reason:
        return False, "kill_switch_active", reason
    reason = _budget_exhausted(cur, tenant_id, work_item_id)
    if reason:
        return False, "budget_exhausted", reason
    return True, "", "ok"


def _degraded_verdict(
    key: tuple[str, str], now_ts: float, exc: Exception
) -> tuple[bool, str, str]:
    """Postgres inacessível: serve o último veredito BOM dentro do hard TTL
    (degradação visível) ou bloqueia (fail-closed). Nunca fail-open cego."""
    global DEGRADED_DECISIONS, UNAVAILABLE_BLOCKS
    cached = _VERDICT_CACHE.get(key)
    if cached is not None:
        verdict, at = cached
        age = now_ts - at
        if age <= HARD_TTL_S:
            DEGRADED_DECISIONS += 1
            logger.warning(
                "dse_budget_hook DEGRADED: postgres inacessível (%s: %s); servindo veredito "
                "de cache com %.0fs de idade para %s (hard TTL %.0fs)",
                type(exc).__name__, str(exc)[:120], age, key, HARD_TTL_S,
            )
            return verdict
    UNAVAILABLE_BLOCKS += 1
    logger.error(
        "dse_budget_hook UNAVAILABLE: postgres inacessível (%s: %s) e sem veredito fresco "
        "para %s — chamada DSE BLOQUEADA (fail-closed)",
        type(exc).__name__, str(exc)[:120], key,
    )
    return (
        False,
        "budget_enforcement_unavailable",
        f"postgres unreachable and no fresh verdict (last: "
        f"{'none' if cached is None else f'{now_ts - cached[1]:.0f}s ago'})",
    )


def evaluate_gate(
    tenant_id: str | None,
    work_item_id: str | None,
    *,
    connect: Callable[[], Any] | None = None,
    now: Callable[[], float] | None = None,
) -> tuple[bool, str, str]:
    """Núcleo puro e testável (sem litellm): retorna (allowed, error, reason).
    Sem contexto DSE → allowed (não é chamada do orquestrador). `connect`/`now`
    são injeção de dependência para os testes das células de degradação."""
    if not tenant_id or not work_item_id:
        return True, "", "no_dse_context"
    key = (tenant_id, work_item_id)
    now_ts = (now or time.monotonic)()
    try:
        conn = (connect or _conn)()
    except Exception as exc:  # noqa: BLE001 — célula "DB fora"
        return _degraded_verdict(key, now_ts, exc)
    try:
        with conn.cursor() as cur:
            verdict = _decide(cur, tenant_id, work_item_id)
    except Exception as exc:  # noqa: BLE001 — query falhou mid-flight
        return _degraded_verdict(key, now_ts, exc)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    _VERDICT_CACHE[key] = (verdict, now_ts)
    return verdict


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
        # 403 kill-switch / 402 budget: recusas de política (P6, não-retryable
        # no client). 503 indisponibilidade de enforcement: RETRYABLE — o blip
        # do Postgres passa e a chamada seguinte decide de verdade.
        status = {"kill_switch_active": 403, "budget_exhausted": 402}.get(error, 503)
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
