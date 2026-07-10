"""Emissão/revogação de virtual keys do LiteLLM por tenant+work_item+stage
(WSD-E1-T3). Esta é a superfície ESTÁVEL que `sandbox_runtime` (WS-C) importa:

    from model_gateway_client import mint_virtual_key, revoke_virtual_key

    key = mint_virtual_key(tenant_id="t1", work_item_id="wi_123", stage=Stage.coder)
    ...
    revoke_virtual_key(key)

Contrato:
  - `mint_virtual_key` chama a API nativa do LiteLLM (`POST /key/generate`)
    com o master key (nunca exposto ao sandbox — só a virtual key emitida
    sai daqui). Grava um registro em `virtual_keys` (migrations/0005_wsd.sql)
    para reconciliação/auditoria e emite uma linha no audit ledger via
    `dse_audit.emit` (P8). Nunca decide budget/policy aqui (isso é
    WSD-E2/Fase 2) — só emite a credencial.
  - `revoke_virtual_key` chama `POST /key/delete`, marca o registro local
    como revogado e também emite audit.
  - Nenhuma das duas funções decide se a emissão "deveria" acontecer — quem
    chama decide (P1: nenhuma decisão de fluxo por LLM; aqui nem é um LLM,
    é o sandbox_runtime determinístico decidindo o lifecycle).
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
    """Valor de retorno rico para quem quiser mais que a string da key (o
    `sandbox_runtime` normalmente só precisa de `.key`)."""

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
    """Emite uma virtual key nova no LiteLLM, escopada a `models` (default:
    todos os models registrados são permitidos se `models=None` — o
    allowlist real por task_class/risk_class é WSD-E2/Fase 2). Retorna a
    string da virtual key (ex.: `sk-...`) — é isso que o sandbox injeta como
    credencial efêmera do Coder para o egress-proxy (WS-C) repassar.
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
    """Revoga uma virtual key: derruba no LiteLLM (chamadas futuras com essa
    key passam a receber 401) e marca `virtual_keys.status='revoked'`
    localmente. Idempotente na parte de negócio: revogar uma key já revogada
    localmente ainda tenta a chamada no LiteLLM (não assume que já foi feita
    lá) mas não falha se o registro local já estiver marcado."""
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
                "revoke_virtual_key chamado com uma key não rastreada em virtual_keys "
                "(nunca emitida por mint_virtual_key deste control-plane, ou já apagada)"
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
