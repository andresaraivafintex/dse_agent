"""Per-stage/per-tenant policy engine (WSD-E2-T1).

DECLARATIVE config (outside the agent's code) mapping
`(tenant, stage, data_class, risk_class)` -> set of allowed models + preferred
model. The master plan's idea: the Coder gets the strongest model, the L2
Reviewer gets the cheapest one — with the agent deciding nothing (P1: no flow
decision made by an LLM; policy is data, not agent code).

Policy sources (order of authority):
  1. The `model_policies` table (migrations/0011_wsd2.sql) — durable and
     hot-reloadable source. An operator does an INSERT/UPDATE here and the
     effect shows up within <TTL seconds with NO redeploy (the engine reads it
     at call time with a short TTL cache).
  2. `load_policies_from_file(path)` — loads a declarative YAML/JSON into the
     table (authoring the config "as code" outside the agent). It is
     convenience sugar; the runtime authority is always the table.

Integration with the tenant's access bundle (WS-F, `dse_access_bundle`): if a
default bundle exists for the tenant and it is `enabled=false`, the tenant is
fully switched off -> no model is allowed (deny-all). Reading the bundle is
defensive: if the WS-F table does not exist yet (parallel build), the engine
degrades to "no additional restriction" and documents that in the README.

Resolution: among the ACTIVE matching rows (with wildcard '*' allowed on any
dimension), the most SPECIFIC one wins (fewest wildcards) and, on a tie, the
one with the highest `priority`. With no applicable row -> permissive policy
(allow), so Phase 1 behavior is not broken when no policy has been configured.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass

from . import db

# Short cache TTL: hot-reload without redeploy, effective in <=_CACHE_TTL seconds.
_CACHE_TTL_SECONDS = float(os.environ.get("DSE_POLICY_CACHE_TTL_SECONDS", "5"))
_WILDCARD = "*"


@dataclass(frozen=True)
class PolicyDecision:
    """Result of policy resolution. `allowed_models=None` means 'no model
    restriction configured' (allow-all) — distinct from an empty list, which
    means deny-all (e.g. tenant switched off in the access bundle)."""

    allowed_models: list[str] | None
    preferred_model: str | None
    source: str  # human-readable description of where the decision came from (audit/debug)

    def permits(self, model: str) -> bool:
        if self.allowed_models is None:
            return True
        return model in self.allowed_models


# --- simple TTL cache (thread-safe) -----------------------------------------
_cache_lock = threading.Lock()
_cache: dict[tuple, tuple[float, list[dict]]] = {}


def _now() -> float:
    return time.monotonic()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _load_active_rows(tenant_id: str) -> list[dict]:
    """Active policy rows applicable to the tenant (the tenant itself + the '*'
    wildcard). Cached with a short TTL so we do not hit Postgres on every model
    call, while keeping hot-reload within <=TTL seconds."""
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
    """Sort key: (num_specific_dimensions, priority). Higher wins."""
    specific = 0
    # every non-wildcard dimension counts as specific (most specific wins)
    for dim in ("tenant_id", "stage", "data_class", "risk_class"):
        if row[dim] != _WILDCARD:
            specific += 1
    return (specific, row["priority"])


def _tenant_disabled_in_access_bundle(tenant_id: str) -> bool:
    """Defensive query against WS-F's access bundle (`dse_access_bundle`): if
    the tenant's DEFAULT bundle (channel IS NULL) exists and is enabled=false,
    the tenant is switched off (deny-all). If the table does not exist yet
    (WS-F built in parallel) or there is no bundle, returns False (no extra
    restriction)."""
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
        # Missing table / schema different from expected: permissive degrade,
        # documented in the README (the WS-F integration is optional/defensive
        # here).
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
    """Resolves the applicable policy. See the module docstring for the
    specificity/wildcard semantics and the access bundle integration."""
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


# --- declarative authoring: load YAML/JSON into the table -------------------
def load_policies_from_file(path: str) -> int:
    """Loads a declarative file (YAML or JSON) into `model_policies` (upsert on
    (tenant_id, stage, data_class, risk_class)). Returns how many rows were
    upserted. Format:

        policies:
          - tenant_id: "*"          # optional (default '*')
            stage: "coder"
            data_class: "*"          # optional
            risk_class: "*"          # optional
            allowed_models: ["bedrock/anthropic.claude-3-5-sonnet"]
            preferred_model: "bedrock/anthropic.claude-3-5-sonnet"
            priority: 10             # optional (default 0)

    This is "config as code" authoring convenience outside the agent; the
    runtime authority remains the table (hot-reload). Called by an operator/CD,
    never by an agent session.
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
        import yaml  # pyyaml is a package dependency (see pyproject.toml)

        return yaml.safe_load(text) or {}
    except ModuleNotFoundError as exc:  # pragma: no cover - defensive
        raise RuntimeError(
            "pyyaml not installed — use a .json file or install the extra"
        ) from exc
