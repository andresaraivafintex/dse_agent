"""WSE-E5-T12 — artifact store Garage (S3 self-hosted, dxflrs/garage pinado no
docker-compose.wse.yml). Implementa a Activity `publish_artifact` do contrato
(`ACTIVITY_PUBLISH_ARTIFACT`, input `PublishArtifactInput`, retorno `ArtifactRef`).

Decisões (documentadas, P7 boring-first):
  - 1 BUCKET POR TENANT (`dse-tenant-<slug>`) + chave prefixada por WorkItem
    (NFR-03: isolamento de evidência por tenant é estrutural, não convenção).
  - Presigned URL com TTL — links de evidência EXPIRAM POR POLÍTICA (exit da
    Fase 3). Provado por teste real: URL expirada retorna negado (403).
  - Upload MULTIPART real (create/upload_part/complete) acima do threshold de
    5 MiB — obrigação do ADR-18 revisado, validada com um vídeo real >5MB.
  - QUARENTENA (costura com WS-F, Fase 2): quando um work item é quarantinado
    (`dse_work_item_quarantine` / `quarantine_work_item()`), os artefatos dele
    são MOVIDOS para o prefixo `quarantine/` e o acesso via as URLs presigned
    antigas é invalidado ANTES do TTL (a chave original deixa de existir).
  - LOG DE ACESSO: toda resolução de link (`resolve_artifact_url`) grava uma
    linha em `wse_artifact_access_log` associável ao PR + audit (P8) — insumo
    da métrica "evidence consumption".

Bootstrap idempotente via admin API (:3903): layout single-node, chave S3 do
serviço, bucket por tenant. Nenhum segredo S3 em env/arquivo — a secret key é
buscada da admin API a cada processo (dev; produção = Vault/ESO, WS-F).
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone

import httpx

from dse_contracts.activities import ArtifactRef, PublishArtifactInput

from dse_validation import db
from dse_validation.config import GarageConfig

try:
    from dse_audit import emit as audit_emit
except ImportError:  # pragma: no cover
    audit_emit = None

logger = logging.getLogger("dse_validation.evidence.garage")

_QUARANTINE_PREFIX = "quarantine/"


def _admin(cfg: GarageConfig, method: str, path: str, json_body=None) -> httpx.Response:
    resp = httpx.request(
        method,
        f"{cfg.admin_endpoint}{path}",
        headers={"Authorization": f"Bearer {cfg.admin_token}"},
        json=json_body,
        timeout=15.0,
    )
    return resp


def _parse_capacity(cap: str) -> int:
    m = re.fullmatch(r"(\d+)\s*([KMGT]?)B?", cap.strip(), re.IGNORECASE)
    if not m:
        raise ValueError(f"capacidade inválida: {cap!r}")
    mult = {"": 1, "K": 10**3, "M": 10**6, "G": 10**9, "T": 10**12}[m.group(2).upper()]
    return int(m.group(1)) * mult


def ensure_layout(cfg: GarageConfig | None = None) -> None:
    """Idempotente: aplica o layout single-node dev se ainda não aplicado."""
    cfg = cfg or GarageConfig()
    status = _admin(cfg, "GET", "/v1/status")
    status.raise_for_status()
    body = status.json()
    if body.get("layoutVersion", 0) >= 1:
        return
    node_id = body["node"]
    resp = _admin(
        cfg,
        "POST",
        "/v1/layout",
        [{"id": node_id, "zone": "dc1", "capacity": _parse_capacity(cfg.layout_capacity), "tags": ["dev"]}],
    )
    resp.raise_for_status()
    layout = _admin(cfg, "GET", "/v1/layout")
    layout.raise_for_status()
    version = layout.json().get("version", 0) + 1
    apply_resp = _admin(cfg, "POST", "/v1/layout/apply", {"version": version})
    apply_resp.raise_for_status()
    logger.info("garage: layout single-node aplicado (version=%s)", version)


def ensure_service_key(cfg: GarageConfig | None = None) -> tuple[str, str]:
    """Garante a chave S3 do serviço; retorna (access_key_id, secret_access_key).
    A secret é lida da admin API (nunca persistida em env/arquivo)."""
    cfg = cfg or GarageConfig()
    resp = _admin(cfg, "GET", "/v1/key")
    resp.raise_for_status()
    key_id = None
    for k in resp.json():
        if k.get("name") == cfg.key_name:
            key_id = k["id"]
            break
    if key_id is None:
        created = _admin(cfg, "POST", "/v1/key", {"name": cfg.key_name})
        created.raise_for_status()
        info = created.json()
        return info["accessKeyId"], info["secretAccessKey"]
    info_resp = _admin(cfg, "GET", f"/v1/key?id={key_id}&showSecretKey=true")
    info_resp.raise_for_status()
    info = info_resp.json()
    return info["accessKeyId"], info["secretAccessKey"]


def _tenant_slug(tenant_id: str) -> str:
    slug = re.sub(r"[^a-z0-9-]", "-", tenant_id.lower())
    return re.sub(r"-+", "-", slug).strip("-")[:48] or "unknown"


def bucket_for_tenant(tenant_id: str, cfg: GarageConfig | None = None) -> str:
    cfg = cfg or GarageConfig()
    return f"{cfg.bucket_prefix}{_tenant_slug(tenant_id)}"


def ensure_tenant_bucket(tenant_id: str, cfg: GarageConfig | None = None) -> str:
    """Idempotente: cria o bucket do tenant (NFR-03) e dá read/write/owner à
    chave do serviço. Retorna o nome do bucket."""
    cfg = cfg or GarageConfig()
    bucket = bucket_for_tenant(tenant_id, cfg)
    resp = _admin(cfg, "GET", f"/v1/bucket?globalAlias={bucket}")
    if resp.status_code == 404:
        created = _admin(cfg, "POST", "/v1/bucket", {"globalAlias": bucket})
        created.raise_for_status()
        bucket_id = created.json()["id"]
    else:
        resp.raise_for_status()
        bucket_id = resp.json()["id"]
    access_key_id, _ = ensure_service_key(cfg)
    allow = _admin(
        cfg,
        "POST",
        "/v1/bucket/allow",
        {
            "bucketId": bucket_id,
            "accessKeyId": access_key_id,
            "permissions": {"read": True, "write": True, "owner": True},
        },
    )
    allow.raise_for_status()
    return bucket


def ensure_garage_ready(cfg: GarageConfig | None = None) -> None:
    """Bootstrap completo idempotente (layout + chave do serviço)."""
    cfg = cfg or GarageConfig()
    ensure_layout(cfg)
    ensure_service_key(cfg)


# ---------------------------------------------------------------------------
# Cliente S3 (boto3) contra o Garage
# ---------------------------------------------------------------------------
def s3_client(cfg: GarageConfig | None = None):
    import boto3
    from botocore.config import Config as BotoConfig

    cfg = cfg or GarageConfig()
    access_key_id, secret = ensure_service_key(cfg)
    return boto3.client(
        "s3",
        endpoint_url=cfg.s3_endpoint,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret,
        region_name=cfg.region,
        config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def _upload(client, bucket: str, key: str, local_path: str, content_type: str, threshold: int) -> tuple[int, bool]:
    """Upload real; MULTIPART explícito (create/upload_part/complete) acima do
    threshold — retorna (size_bytes, multipart_used)."""
    size = os.path.getsize(local_path)
    if size <= threshold:
        with open(local_path, "rb") as fh:
            client.put_object(Bucket=bucket, Key=key, Body=fh, ContentType=content_type)
        return size, False

    mpu = client.create_multipart_upload(Bucket=bucket, Key=key, ContentType=content_type)
    upload_id = mpu["UploadId"]
    parts = []
    try:
        with open(local_path, "rb") as fh:
            part_number = 1
            while True:
                chunk = fh.read(threshold)
                if not chunk:
                    break
                resp = client.upload_part(
                    Bucket=bucket, Key=key, PartNumber=part_number, UploadId=upload_id, Body=chunk
                )
                parts.append({"ETag": resp["ETag"], "PartNumber": part_number})
                part_number += 1
        client.complete_multipart_upload(
            Bucket=bucket, Key=key, UploadId=upload_id, MultipartUpload={"Parts": parts}
        )
    except Exception:
        client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
        raise
    return size, True


def presign_get(client, bucket: str, key: str, ttl_seconds: int) -> str:
    return client.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=ttl_seconds
    )


# ---------------------------------------------------------------------------
# publish_artifact — core da Activity do contrato
# ---------------------------------------------------------------------------
def publish_artifact_core(
    inp: PublishArtifactInput,
    *,
    cfg: GarageConfig | None = None,
    actor: str = "system:validation",
) -> ArtifactRef:
    cfg = cfg or GarageConfig()
    ensure_layout(cfg)
    bucket = ensure_tenant_bucket(inp.tenant_id, cfg)
    client = s3_client(cfg)

    filename = os.path.basename(inp.local_path)
    store_key = f"{inp.work_item_id}/{inp.kind}/{filename}"
    size, multipart = _upload(
        client, bucket, store_key, inp.local_path, inp.content_type, cfg.multipart_threshold_bytes
    )
    url = presign_get(client, bucket, store_key, inp.ttl_seconds)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=inp.ttl_seconds)

    db.record_artifact(
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        kind=inp.kind,
        bucket=bucket,
        store_key=store_key,
        content_type=inp.content_type,
        size_bytes=size,
        multipart=multipart,
        ttl_seconds=inp.ttl_seconds,
        expires_at=expires_at,
    )
    if audit_emit is not None:
        audit_emit(
            actor=actor,
            action="artifact_published",
            tenant_id=inp.tenant_id,
            work_item_id=inp.work_item_id,
            details={
                "kind": inp.kind,
                "bucket": bucket,
                "store_key": store_key,
                "size_bytes": size,
                "multipart": multipart,
                "ttl_seconds": inp.ttl_seconds,
                "expires_at": expires_at.isoformat(),
            },
        )
    return ArtifactRef(
        work_item_id=inp.work_item_id,
        tenant_id=inp.tenant_id,
        kind=inp.kind,
        store_key=store_key,
        presigned_url=url,
        expires_at=expires_at.isoformat(),
    )


# ---------------------------------------------------------------------------
# Resolução de link com LOG DE ACESSO (métrica evidence consumption)
# ---------------------------------------------------------------------------
def resolve_artifact_url(
    *,
    work_item_id: str,
    store_key: str,
    accessor: str,
    pr_number: int | None = None,
    via: str = "presign",
    ttl_seconds: int | None = None,
    cfg: GarageConfig | None = None,
) -> str:
    """Gera uma presigned URL fresca para um artefato JÁ publicado e registra o
    acesso (tabela própria + audit, associável ao PR). Recusa artefato
    quarantinado ou expirado por política (P6 — falha limpa, nunca um link
    'meio válido')."""
    cfg = cfg or GarageConfig()
    row = db.get_artifact(work_item_id, store_key)
    if row is None:
        raise LookupError(f"artefato não publicado: {work_item_id}/{store_key}")
    if row["quarantined_at"] is not None:
        raise PermissionError(
            f"artefato em quarentena (work item quarantinado): {work_item_id}/{store_key}"
        )
    now = datetime.now(timezone.utc)
    if row["expires_at"] is not None and now >= row["expires_at"]:
        raise PermissionError(f"artefato expirado por política: {work_item_id}/{store_key}")

    remaining = int((row["expires_at"] - now).total_seconds()) if row["expires_at"] else 3600
    ttl = min(ttl_seconds or remaining, max(1, remaining))
    client = s3_client(cfg)
    url = presign_get(client, row["bucket"], store_key, ttl)

    db.record_artifact_access(
        artifact_id=row["id"],
        work_item_id=work_item_id,
        tenant_id=row["tenant_id"],
        pr_number=pr_number,
        accessor=accessor,
        via=via,
    )
    if audit_emit is not None:
        audit_emit(
            actor=accessor,
            action="artifact_link_resolved",
            tenant_id=row["tenant_id"],
            work_item_id=work_item_id,
            details={"store_key": store_key, "pr_number": pr_number, "via": via},
        )
    return url


# ---------------------------------------------------------------------------
# QUARENTENA — costura com WS-F (dse_work_item_quarantine, Fase 2)
# ---------------------------------------------------------------------------
def quarantine_artifacts_for_work_item(
    work_item_id: str,
    *,
    actor: str = "system:validation",
    cfg: GarageConfig | None = None,
) -> list[str]:
    """Move TODOS os artefatos do work item para o prefixo `quarantine/` e
    invalida o acesso antes do TTL (a chave original deixa de existir no
    bucket => qualquer presigned URL antiga passa a retornar negado). Chamada
    quando o work item é quarantinado (WS-F: `quarantine_work_item()` /
    tabela `dse_work_item_quarantine`). Idempotente. Retorna as chaves movidas."""
    cfg = cfg or GarageConfig()
    rows = db.list_artifacts(work_item_id)
    if not rows:
        return []
    client = s3_client(cfg)
    moved: list[str] = []
    for row in rows:
        if row["quarantined_at"] is not None:
            continue
        src_key = row["store_key"]
        dst_key = f"{_QUARANTINE_PREFIX}{src_key}"
        client.copy_object(
            Bucket=row["bucket"],
            Key=dst_key,
            CopySource={"Bucket": row["bucket"], "Key": src_key},
        )
        client.delete_object(Bucket=row["bucket"], Key=src_key)
        db.mark_artifact_quarantined(row["id"], quarantine_key=dst_key)
        moved.append(src_key)
        if audit_emit is not None:
            audit_emit(
                actor=actor,
                action="artifact_quarantined",
                tenant_id=row["tenant_id"],
                work_item_id=work_item_id,
                details={"store_key": src_key, "quarantine_key": dst_key, "bucket": row["bucket"]},
            )
    return moved


def sweep_quarantined_work_items(*, actor: str = "system:validation", cfg: GarageConfig | None = None) -> dict[str, list[str]]:
    """Varredura determinística: para cada work item ATIVO em quarentena
    (`dse_work_item_quarantine`, tabela do WS-F) que ainda tem artefato fora do
    prefixo de quarentena, move e invalida. Pode rodar em cron/Activity — o
    aceite do WS-F é que artefato de work item quarantinado fica inacessível."""
    cfg = cfg or GarageConfig()
    result: dict[str, list[str]] = {}
    for wi in db.list_quarantined_work_items_with_artifacts():
        moved = quarantine_artifacts_for_work_item(wi, actor=actor, cfg=cfg)
        if moved:
            result[wi] = moved
    return result
