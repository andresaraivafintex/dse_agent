"""Kill switch de gateway por escopo + reassign de modelo em voo (WSD-E4-T2).

Kill switch — matriz de 4 escopos (global | tenant | work_item | channel):
  - o gateway ENFORÇA no call time os escopos visíveis nos headers da chamada:
    global, tenant, work_item;
  - `channel` não é visível no gateway (o header não carrega o canal) — linhas
    de escopo channel são honradas na ADMISSÃO pelo WS-A/WS-B; ficam nesta
    tabela para operabilidade unificada, documentado no README.

"Conecta aos controles do WS-B/WS-F": o check lê TAMBÉM as tabelas dos outros
workstreams, então um operador que aciona o kill switch por qualquer um dos
caminhos para as chamadas de modelo:
  - global : `gateway_kill_switches(scope='global')` OU `dse_kill_switch_global`
             (WS-F, WSF-E6-T2);
  - tenant : `gateway_kill_switches(scope='tenant')` OU
             `tenant_config.kill_switch_enabled` (WS-F);
  - work_item: `gateway_kill_switches(scope='work_item')`.

Efeito <60s: o check lê com cache TTL curto (default 5s), muito abaixo de 60s —
uma vez acionado, a próxima chamada do escopo (em <=TTL s) já é recusada. Um
kill NÃO interrompe uma geração já em andamento (não há stream aqui); ele zera
a EMISSÃO de novas chamadas do escopo, que é o requisito.

Reassign de modelo em voo: um operador troca o modelo efetivo de um WorkItem;
a próxima chamada daquele work_item usa `to_model` no lugar do requisitado. O
reassign NÃO burla a política (o modelo efetivo ainda passa por `policy.py`).

Toda mutação de operador (kill on/off, reassign set/clear) gera linha de audit
via `dse_audit.emit` (P8).
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

from dse_audit.client import emit as audit_emit

from . import db

_AUDIT_ACTOR_DEFAULT = "system:model-gateway"
_CACHE_TTL_SECONDS = float(os.environ.get("DSE_CONTROLS_CACHE_TTL_SECONDS", "5"))

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, object]] = {}


def _now() -> float:
    return time.monotonic()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


@dataclass(frozen=True)
class KillDecision:
    scope_type: str
    scope_id: str
    reason: str | None
    source: str  # tabela/coluna de onde veio (auditoria)


# ---------------------------------------------------------------------------
# Kill switch — leitura (call time)
# ---------------------------------------------------------------------------
def _load_kill_state() -> dict:
    """Snapshot cacheado de todo o estado de kill relevante (gateway + WS-F).
    Uma leitura por TTL evita N queries por chamada de modelo."""
    with _cache_lock:
        hit = _cache.get("kill_state")
        if hit is not None and (_now() - hit[0]) < _CACHE_TTL_SECONDS:
            return hit[1]  # type: ignore[return-value]

    state = {
        "gateway": {},  # (scope_type, scope_id) -> reason
        "global_wsf": None,  # reason ou None
        "tenant_wsf": {},  # tenant_id -> reason
    }
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT scope_type, scope_id, reason FROM gateway_kill_switches WHERE enabled"
            )
            for scope_type, scope_id, reason in cur.fetchall():
                state["gateway"][(scope_type, scope_id)] = reason

        # WS-F: global kill switch (defensivo — tabela pode não existir ainda).
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "SELECT reason FROM dse_kill_switch_global WHERE id = 'global' AND enabled"
                )
                row = cur.fetchone()
                if row is not None:
                    state["global_wsf"] = row[0] or "global kill switch (WS-F)"
            except Exception:
                conn.rollback()

        # WS-F: kill switch por tenant (tenant_config).
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "SELECT tenant_id, kill_switch_reason FROM tenant_config WHERE kill_switch_enabled"
                )
                for tenant_id, reason in cur.fetchall():
                    state["tenant_wsf"][tenant_id] = reason or "tenant kill switch (WS-F)"
            except Exception:
                conn.rollback()
    finally:
        conn.close()

    with _cache_lock:
        _cache["kill_state"] = (_now(), state)
    return state


def is_killed(tenant_id: str, work_item_id: str) -> KillDecision | None:
    """Retorna a primeira decisão de kill aplicável (ou None). Ordem: global,
    tenant, work_item — a mais ampla primeiro."""
    st = _load_kill_state()

    # global
    if ("global", "*") in st["gateway"]:
        return KillDecision("global", "*", st["gateway"][("global", "*")], "gateway_kill_switches")
    if st["global_wsf"] is not None:
        return KillDecision("global", "*", st["global_wsf"], "dse_kill_switch_global")

    # tenant
    if ("tenant", tenant_id) in st["gateway"]:
        return KillDecision(
            "tenant", tenant_id, st["gateway"][("tenant", tenant_id)], "gateway_kill_switches"
        )
    if tenant_id in st["tenant_wsf"]:
        return KillDecision("tenant", tenant_id, st["tenant_wsf"][tenant_id], "tenant_config")

    # work_item
    if ("work_item", work_item_id) in st["gateway"]:
        return KillDecision(
            "work_item",
            work_item_id,
            st["gateway"][("work_item", work_item_id)],
            "gateway_kill_switches",
        )
    return None


# ---------------------------------------------------------------------------
# Kill switch — mutação (operador WS-B/WS-F)
# ---------------------------------------------------------------------------
def set_kill_switch(
    scope_type: str,
    scope_id: str = "*",
    *,
    enabled: bool = True,
    reason: str | None = None,
    actor: str = _AUDIT_ACTOR_DEFAULT,
    tenant_id: str | None = None,
) -> None:
    """Liga/desliga o kill switch do gateway para um escopo. Idempotente.
    Emite audit. `scope_type` in {global, tenant, work_item, channel}."""
    if scope_type not in ("global", "tenant", "work_item", "channel"):
        raise ValueError(f"scope_type inválido: {scope_type}")
    if scope_type == "global":
        scope_id = "*"

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO gateway_kill_switches (scope_type, scope_id, enabled, reason, actor)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (scope_type, scope_id)
                DO UPDATE SET enabled = EXCLUDED.enabled,
                              reason = EXCLUDED.reason,
                              actor = EXCLUDED.actor
                """,
                (scope_type, scope_id, enabled, reason, actor),
            )
        conn.commit()
    finally:
        conn.close()

    clear_cache()
    audit_emit(
        actor=actor,
        action="gateway.kill_switch_set" if enabled else "gateway.kill_switch_cleared",
        tenant_id=tenant_id or (scope_id if scope_type == "tenant" else "*"),
        work_item_id=scope_id if scope_type == "work_item" else None,
        details={"scope_type": scope_type, "scope_id": scope_id, "enabled": enabled, "reason": reason},
    )


# ---------------------------------------------------------------------------
# Reassign de modelo em voo
# ---------------------------------------------------------------------------
def resolve_reassignment(work_item_id: str) -> str | None:
    """Modelo para o qual o work_item foi reassignado (ou None). Cacheado por
    TTL curto (mesma janela <60s do kill switch)."""
    key = f"reassign:{work_item_id}"
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and (_now() - hit[0]) < _CACHE_TTL_SECONDS:
            return hit[1]  # type: ignore[return-value]

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT to_model FROM model_reassignments WHERE work_item_id = %s AND is_active",
                (work_item_id,),
            )
            row = cur.fetchone()
            to_model = row[0] if row else None
    finally:
        conn.close()

    with _cache_lock:
        _cache[key] = (_now(), to_model)
    return to_model


def reassign_model(
    work_item_id: str,
    to_model: str,
    *,
    reason: str | None = None,
    actor: str = _AUDIT_ACTOR_DEFAULT,
    tenant_id: str = "*",
) -> None:
    """Reassigna o modelo efetivo de um WorkItem em voo. Desativa qualquer
    reassignment ativo anterior e cria a nova (no máximo uma ativa por
    work_item). Emite audit."""
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE model_reassignments SET is_active = false "
                "WHERE work_item_id = %s AND is_active",
                (work_item_id,),
            )
            cur.execute(
                """
                INSERT INTO model_reassignments (work_item_id, to_model, reason, actor, is_active)
                VALUES (%s, %s, %s, %s, true)
                """,
                (work_item_id, to_model, reason, actor),
            )
        conn.commit()
    finally:
        conn.close()

    clear_cache()
    audit_emit(
        actor=actor,
        action="gateway.model_reassigned",
        tenant_id=tenant_id,
        work_item_id=work_item_id,
        details={"to_model": to_model, "reason": reason},
    )


def clear_reassignment(
    work_item_id: str, *, actor: str = _AUDIT_ACTOR_DEFAULT, tenant_id: str = "*"
) -> None:
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE model_reassignments SET is_active = false "
                "WHERE work_item_id = %s AND is_active",
                (work_item_id,),
            )
        conn.commit()
    finally:
        conn.close()

    clear_cache()
    audit_emit(
        actor=actor,
        action="gateway.model_reassignment_cleared",
        tenant_id=tenant_id,
        work_item_id=work_item_id,
        details={},
    )
