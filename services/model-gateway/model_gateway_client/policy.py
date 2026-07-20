"""Motor de política per-stage/per-tenant (WSD-E2-T1).

Config DECLARATIVA (fora do código do agente) que mapeia
`(tenant, stage, data_class, risk_class)` -> conjunto de modelos permitidos +
modelo preferido. A ideia do plano mestre: o Coder recebe o modelo mais forte,
o Reviewer L2 recebe o mais barato — sem que o agente decida nada (P1: nenhuma
decisão de fluxo por LLM; a política é dados, não código do agente).

Fontes de política (ordem de autoridade):
  1. Tabela `model_policies` (migrations/0011_wsd2.sql) — fonte durável e
     hot-reloadable. Um operador dá INSERT/UPDATE aqui e o efeito aparece em
     <TTL segundos SEM redeploy (o motor lê no call time com cache TTL curto).
  2. `load_policies_from_file(path)` — carrega um YAML/JSON declarativo para a
     tabela (autoria da config "as code" fora do agente). É açúcar de
     conveniência; a autoridade em runtime é sempre a tabela.

Integração com o access bundle do tenant (WS-F, `dse_access_bundle`): se existe
um bundle default do tenant e ele está `enabled=false`, o tenant está
totalmente desligado -> nenhum modelo é permitido (deny-all). Ler o bundle é
defensivo: se a tabela do WS-F ainda não existir (build em paralelo), o motor
degrada para "sem restrição adicional" e documenta no README.

Resolução: entre as linhas ATIVAS que casam (com coringa '*' permitido em
qualquer dimensão), vence a mais ESPECÍFICA (menos coringas) e, no empate, a de
maior `priority`. Sem nenhuma linha aplicável -> política permissiva (allow),
para não quebrar o comportamento da Fase 1 quando nenhuma política foi
configurada.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass

from . import db

# Cache TTL curto: hot-reload sem redeploy, efeito em <=_CACHE_TTL segundos.
_CACHE_TTL_SECONDS = float(os.environ.get("DSE_POLICY_CACHE_TTL_SECONDS", "5"))
_WILDCARD = "*"


@dataclass(frozen=True)
class PolicyDecision:
    """Resultado da resolução de política. `allowed_models=None` significa
    'sem restrição de modelo configurada' (allow-all) — distinto de uma lista
    vazia, que significa deny-all (ex.: tenant desligado no access bundle)."""

    allowed_models: list[str] | None
    preferred_model: str | None
    source: str  # descrição legível de onde a decisão veio (auditoria/debug)

    def permits(self, model: str) -> bool:
        if self.allowed_models is None:
            return True
        return model in self.allowed_models


# --- cache simples com TTL (thread-safe) ------------------------------------
_cache_lock = threading.Lock()
_cache: dict[tuple, tuple[float, list[dict]]] = {}


def _now() -> float:
    return time.monotonic()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _load_active_rows(tenant_id: str) -> list[dict]:
    """Linhas de política ativas aplicáveis ao tenant (o próprio tenant + o
    coringa '*'). Cacheado por TTL curto para não bater no Postgres a cada
    chamada de modelo, mantendo o hot-reload em <=TTL segundos."""
    cache_key = ("policies", tenant_id)
    with _cache_lock:
        hit = _cache.get(cache_key)
        if hit is not None and (_now() - hit[0]) < _CACHE_TTL_SECONDS:
            return hit[1]

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT tenant_id, stage, data_class, risk_class,
                       allowed_models, preferred_model, priority
                  FROM model_policies
                 WHERE is_active AND tenant_id IN (%s, %s)
                """,
                (tenant_id, _WILDCARD),
            )
            rows = [
                {
                    "tenant_id": r[0],
                    "stage": r[1],
                    "data_class": r[2],
                    "risk_class": r[3],
                    "allowed_models": list(r[4]) if r[4] is not None else [],
                    "preferred_model": r[5],
                    "priority": int(r[6]),
                }
                for r in cur.fetchall()
            ]
    finally:
        conn.close()

    with _cache_lock:
        _cache[cache_key] = (_now(), rows)
    return rows


def _matches(row_value: str, actual: str) -> bool:
    return row_value == _WILDCARD or row_value == actual


def _specificity(row: dict) -> tuple[int, int]:
    """Chave de ordenação: (num_dimensões_específicas, priority). Maior vence."""
    specific = 0
    # conta como específica cada dimensão que não é coringa (mais específica vence)
    for dim in ("tenant_id", "stage", "data_class", "risk_class"):
        if row[dim] != _WILDCARD:
            specific += 1
    return (specific, row["priority"])


def _tenant_disabled_in_access_bundle(tenant_id: str) -> bool:
    """Consulta defensiva ao access bundle do WS-F (`dse_access_bundle`): se o
    bundle DEFAULT do tenant (channel IS NULL) existe e está enabled=false, o
    tenant está desligado (deny-all). Se a tabela ainda não existe (WS-F em
    build paralelo) ou não há bundle, retorna False (sem restrição extra)."""
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT enabled FROM dse_access_bundle
                 WHERE tenant_id = %s AND channel IS NULL
                 LIMIT 1
                """,
                (tenant_id,),
            )
            row = cur.fetchone()
            if row is None:
                return False
            return row[0] is False
    except Exception:
        # Tabela ausente / esquema diferente do esperado: degrade permissivo,
        # documentado no README (integração WS-F é opcional/defensiva aqui).
        conn.rollback()
        return False
    finally:
        conn.close()


def resolve_policy(
    tenant_id: str,
    stage: str,
    *,
    data_class: str = "internal",
    risk_class: str = "*",
) -> PolicyDecision:
    """Resolve a política aplicável. Ver docstring do módulo para a semântica
    de especificidade/coringa e da integração com o access bundle."""
    if _tenant_disabled_in_access_bundle(tenant_id):
        return PolicyDecision(
            allowed_models=[],
            preferred_model=None,
            source=f"access_bundle_disabled(tenant={tenant_id})",
        )

    rows = _load_active_rows(tenant_id)
    applicable = [
        r
        for r in rows
        if _matches(r["tenant_id"], tenant_id)
        and _matches(r["stage"], stage)
        and _matches(r["data_class"], data_class)
        and _matches(r["risk_class"], risk_class)
    ]
    if not applicable:
        return PolicyDecision(
            allowed_models=None,
            preferred_model=None,
            source="no_policy_configured(allow_all)",
        )

    best = max(applicable, key=_specificity)
    return PolicyDecision(
        allowed_models=list(best["allowed_models"]),
        preferred_model=best["preferred_model"],
        source=(
            f"model_policies(tenant={best['tenant_id']},stage={best['stage']},"
            f"data_class={best['data_class']},risk_class={best['risk_class']})"
        ),
    )


# --- autoria declarativa: carregar YAML/JSON para a tabela ------------------
def load_policies_from_file(path: str) -> int:
    """Carrega um arquivo declarativo (YAML ou JSON) para `model_policies`
    (upsert por (tenant_id, stage, data_class, risk_class)). Retorna quantas
    linhas foram upsertadas. Formato:

        policies:
          - tenant_id: "*"          # opcional (default '*')
            stage: "coder"
            data_class: "*"          # opcional
            risk_class: "*"          # opcional
            allowed_models: ["bedrock/anthropic.claude-3-5-sonnet"]
            preferred_model: "bedrock/anthropic.claude-3-5-sonnet"
            priority: 10             # opcional (default 0)

    Isto é conveniência de autoria "config as code" fora do agente; a
    autoridade em runtime continua sendo a tabela (hot-reload). Chamado por
    um operador/CD, nunca por uma sessão de agente.
    """
    data = _read_config_file(path)
    entries = data.get("policies", []) if isinstance(data, dict) else []
    upserted = 0
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            for e in entries:
                cur.execute(
                    """
                    INSERT INTO model_policies
                        (tenant_id, stage, data_class, risk_class,
                         allowed_models, preferred_model, priority, is_active)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, true)
                    ON CONFLICT (tenant_id, stage, data_class, risk_class)
                    DO UPDATE SET allowed_models = EXCLUDED.allowed_models,
                                  preferred_model = EXCLUDED.preferred_model,
                                  priority = EXCLUDED.priority,
                                  is_active = true
                    """,
                    (
                        str(e.get("tenant_id", _WILDCARD)),
                        str(e["stage"]),
                        str(e.get("data_class", _WILDCARD)),
                        str(e.get("risk_class", _WILDCARD)),
                        json.dumps(list(e["allowed_models"])),
                        e.get("preferred_model"),
                        int(e.get("priority", 0)),
                    ),
                )
                upserted += 1
        conn.commit()
    finally:
        conn.close()
    clear_cache()
    return upserted


def _read_config_file(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    if path.endswith(".json"):
        return json.loads(text)
    try:
        import yaml  # pyyaml é dependência do pacote (ver pyproject.toml)

        return yaml.safe_load(text) or {}
    except ModuleNotFoundError as exc:  # pragma: no cover - defensivo
        raise RuntimeError(
            "pyyaml não instalado — use um arquivo .json ou instale o extra"
        ) from exc
