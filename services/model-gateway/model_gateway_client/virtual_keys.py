"""Issue/revoke LiteLLM virtual keys per tenant+work_item+stage (WSD-E1-T3).
This is the STABLE surface that `sandbox_runtime` (WS-C) imports:

    from model_gateway_client import mint_virtual_key, revoke_virtual_key

    key = mint_virtual_key(tenant_id="t1", work_item_id="wi_123", stage=Stage.coder)
    ...
    revoke_virtual_key(key)

Contract:
  - `mint_virtual_key` calls LiteLLM's native API (`POST /key/generate`) with
    the master key (never exposed to the sandbox — only the issued virtual key
    leaves this module). It writes a row into `virtual_keys`
    (migrations/0005_wsd.sql) for reconciliation/auditing and emits a line on
    the audit ledger via `dse_audit.emit` (P8). It never decides budget/policy
    here (that is WSD-E2/Phase 2) — it only issues the credential.
  - `revoke_virtual_key` calls `POST /key/delete`, marks the local row as
    revoked and emits audit as well.
  - Neither function decides whether the issuance "should" happen — the caller
    decides (P1: no flow decision made by an LLM; here it is not even an LLM,
    it is the deterministic sandbox_runtime driving the lifecycle).
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

import httpx
from dse_audit.client import emit as audit_emit
from dse_contracts.gateway_contract import Stage

from . import db, settings
from .errors import GatewayCallError, VirtualKeyNotFoundError

_AUDIT_ACTOR = "system:model-gateway"


@dataclass(frozen=True)
class IssuedVirtualKey:
    """Rich return value for callers that want more than the key string
    (`sandbox_runtime` usually only needs `.key`)."""

    key: str
    key_alias: str
    tenant_id: str
    work_item_id: str
    stage: str


def _stage_value(stage: Stage | str) -> str:
    return stage.value if isinstance(stage, Stage) else str(stage)


def _admin_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.litellm_admin_master_key()}",
        "Content-Type": "application/json",
    }


def mint_virtual_key(
    tenant_id: str,
    work_item_id: str,
    stage: Stage | str,
    *,
    models: list[str] | None = None,
    max_budget_usd: float | None = None,
    ttl_seconds: int | None = None,
) -> str:
    """Issues a fresh virtual key on LiteLLM, scoped to `models` (default: all
    registered models are allowed when `models=None` — the real allowlist per
    task_class/risk_class is WSD-E2/Phase 2). Returns the virtual key string
    (e.g. `sk-...`) — that is what the sandbox injects as the Coder's ephemeral
    credential for the egress-proxy (WS-C) to forward.
    """
    stage_str = _stage_value(stage)
    key_alias = f"{tenant_id}--{work_item_id}--{stage_str}--{uuid.uuid4().hex[:8]}"

    body: dict = {
        "key_alias": key_alias,
        "metadata": {
            "tenant_id": tenant_id,
            "work_item_id": work_item_id,
            "stage": stage_str,
        },
    }
    if models is not None:
        body["models"] = models
    if max_budget_usd is not None:
        body["max_budget"] = max_budget_usd
    if ttl_seconds is not None:
        body["duration"] = f"{ttl_seconds}s"

    url = f"{settings.gateway_base_url()}/key/generate"
    try:
        resp = httpx.post(url, json=body, headers=_admin_headers(), timeout=10.0)
    except httpx.HTTPError as exc:
        audit_emit(
            actor=_AUDIT_ACTOR,
            action="virtual_key.issue_failed",
            tenant_id=tenant_id,
            work_item_id=work_item_id,
            details={"stage": stage_str, "key_alias": key_alias, "error": str(exc)},
        )
        raise GatewayCallError(0, {"error": "transport_error", "message": str(exc)}) from exc

    if resp.status_code >= 300:
        audit_emit(
            actor=_AUDIT_ACTOR,
            action="virtual_key.issue_failed",
            tenant_id=tenant_id,
            work_item_id=work_item_id,
            details={"stage": stage_str, "key_alias": key_alias, "status_code": resp.status_code, "body": resp.text},
        )
        raise GatewayCallError(resp.status_code, _safe_json(resp))

    payload = resp.json()
    virtual_key = payload["key"]
    key_hash = hashlib.sha256(virtual_key.encode("utf-8")).hexdigest()
    key_prefix = virtual_key[:12]

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO virtual_keys
                    (tenant_id, work_item_id, stage, key_alias, key_hash, key_prefix, status)
                VALUES (%s, %s, %s, %s, %s, %s, 'active')
                """,
                (tenant_id, work_item_id, stage_str, key_alias, key_hash, key_prefix),
            )
        conn.commit()
    finally:
        conn.close()

    audit_emit(
        actor=_AUDIT_ACTOR,
        action="virtual_key.issued",
        tenant_id=tenant_id,
        work_item_id=work_item_id,
        details={
            "stage": stage_str,
            "key_alias": key_alias,
            "key_prefix": key_prefix,
            "models": models,
            "max_budget_usd": max_budget_usd,
            "ttl_seconds": ttl_seconds,
        },
    )
    return virtual_key


def revoke_virtual_key(key: str) -> None:
    """Revokes a virtual key: kills it on LiteLLM (future calls with that key
    start getting 401) and marks `virtual_keys.status='revoked'` locally.
    Idempotent on the business side: revoking a key already revoked locally
    still attempts the LiteLLM call (it does not assume it already happened
    there) but does not fail if the local row is already marked."""
    key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tenant_id, work_item_id, stage, key_alias, key_prefix, status "
                "FROM virtual_keys WHERE key_hash = %s",
                (key_hash,),
            )
            row = cur.fetchone()
        if row is None:
            raise VirtualKeyNotFoundError(
                "revoke_virtual_key called with a key not tracked in virtual_keys "
                "(never issued by this control-plane's mint_virtual_key, or already deleted)"
            )
        tenant_id, work_item_id, stage_str, key_alias, key_prefix, status = row

        url = f"{settings.gateway_base_url()}/key/delete"
        try:
            resp = httpx.post(url, json={"keys": [key]}, headers=_admin_headers(), timeout=10.0)
        except httpx.HTTPError as exc:
            audit_emit(
                actor=_AUDIT_ACTOR,
                action="virtual_key.revoke_failed",
                tenant_id=tenant_id,
                work_item_id=work_item_id,
                details={"stage": stage_str, "key_alias": key_alias, "error": str(exc)},
            )
            raise GatewayCallError(0, {"error": "transport_error", "message": str(exc)}) from exc

        if resp.status_code >= 300:
            audit_emit(
                actor=_AUDIT_ACTOR,
                action="virtual_key.revoke_failed",
                tenant_id=tenant_id,
                work_item_id=work_item_id,
                details={"stage": stage_str, "key_alias": key_alias, "status_code": resp.status_code, "body": resp.text},
            )
            raise GatewayCallError(resp.status_code, _safe_json(resp))

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE virtual_keys SET status = 'revoked', revoked_at = now() "
                "WHERE key_hash = %s AND status = 'active'",
                (key_hash,),
            )
        conn.commit()
    finally:
        conn.close()

    audit_emit(
        actor=_AUDIT_ACTOR,
        action="virtual_key.revoked",
        tenant_id=tenant_id,
        work_item_id=work_item_id,
        details={"stage": stage_str, "key_alias": key_alias, "key_prefix": key_prefix},
    )


def _safe_json(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except Exception:
        return {"raw": resp.text}
