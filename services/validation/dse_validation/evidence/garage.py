"""WSE-E5-T12 — Garage artifact store (self-hosted S3, dxflrs/garage pinned in
docker-compose.wse.yml). Implements the contract's `publish_artifact` Activity
(`ACTIVITY_PUBLISH_ARTIFACT`, input `PublishArtifactInput`, returns `ArtifactRef`).

Decisions (documented, P7 boring-first):
  - 1 BUCKET PER TENANT (`dse-tenant-<slug>`) + key prefixed by WorkItem
    (NFR-03: per-tenant evidence isolation is structural, not a convention).
  - Presigned URL with a TTL — evidence links EXPIRE BY POLICY (Phase 3 exit
    criterion). Proven by a real test: an expired URL returns denied (403).
  - Real MULTIPART upload (create/upload_part/complete) above the 5 MiB threshold
    — required by the revised ADR-18, validated with a real >5MB video.
  - QUARANTINE (seam with WS-F, Phase 2): when a work item is quarantined
    (`dse_work_item_quarantine` / `quarantine_work_item()`), its artifacts are
    MOVED to the `quarantine/` prefix and access through the old presigned URLs is
    invalidated BEFORE the TTL (the original key stops existing).
  - ACCESS LOG: every link resolution (`resolve_artifact_url`) writes a row to
    `wse_artifact_access_log` attributable to the PR + audit (P8) — input to the
    "evidence consumption" metric.

Idempotent bootstrap via the admin API (:3903): single-node layout, the service's
S3 key, per-tenant bucket. No S3 secret in env/file — the secret key is fetched
from the admin API on every process (dev; production = Vault/ESO, WS-F).
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
        raise ValueError(f"invalid capacity: {cap!r}")
    mult = {"": 1, "K": 10**3, "M": 10**6, "G": 10**9, "T": 10**12}[m.group(2).upper()]
    return int(m.group(1)) * mult


def ensure_layout(cfg: GarageConfig | None = None) -> None:
    """Idempotent: applies the single-node dev layout if not applied yet."""
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
    """Ensures the service's S3 key; returns (access_key_id, secret_access_key).
    The secret is read from the admin API (never persisted to env/file)."""
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
    """Idempotent: creates the tenant's bucket (NFR-03) and grants read/write/owner
    to the service key. Returns the bucket name."""
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
    """Full idempotent bootstrap (layout + service key)."""
    cfg = cfg or GarageConfig()
    ensure_layout(cfg)
    ensure_service_key(cfg)


# ---------------------------------------------------------------------------
# S3 client (boto3) against Garage
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
    """Real upload; explicit MULTIPART (create/upload_part/complete) above the
    threshold — returns (size_bytes, multipart_used)."""
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
# publish_artifact — core of the contract's Activity
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
# Link resolution with ACCESS LOG (evidence-consumption metric)
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
    """Generates a fresh presigned URL for an ALREADY published artifact and
    records the access (dedicated table + audit, attributable to the PR). Refuses
    a quarantined or policy-expired artifact (P6 — clean failure, never a
    'half-valid' link)."""
    cfg = cfg or GarageConfig()
    row = db.get_artifact(work_item_id, store_key)
    if row is None:
        raise LookupError(f"artifact not published: {work_item_id}/{store_key}")
    if row["quarantined_at"] is not None:
        raise PermissionError(
            f"artifact quarantined (work item under quarantine): {work_item_id}/{store_key}"
        )
    now = datetime.now(timezone.utc)
    if row["expires_at"] is not None and now >= row["expires_at"]:
        raise PermissionError(f"artifact expired by policy: {work_item_id}/{store_key}")

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
# QUARANTINE — seam with WS-F (dse_work_item_quarantine, Phase 2)
# ---------------------------------------------------------------------------
def quarantine_artifacts_for_work_item(
    work_item_id: str,
    *,
    actor: str = "system:validation",
    cfg: GarageConfig | None = None,
) -> list[str]:
    """Moves ALL of the work item's artifacts to the `quarantine/` prefix and
    invalidates access before the TTL (the original key stops existing in the
    bucket => any old presigned URL starts returning denied). Called when the work
    item is quarantined (WS-F: `quarantine_work_item()` /
    `dse_work_item_quarantine` table). Idempotent. Returns the moved keys."""
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
    """Deterministic sweep: for every ACTIVE quarantined work item
    (`dse_work_item_quarantine`, WS-F's table) that still has an artifact outside
    the quarantine prefix, move and invalidate it. Can run on a cron/Activity —
    WS-F's acceptance criterion is that a quarantined work item's artifact becomes
    inaccessible."""
    cfg = cfg or GarageConfig()
    result: dict[str, list[str]] = {}
    for wi in db.list_quarantined_work_items_with_artifacts():
        moved = quarantine_artifacts_for_work_item(wi, actor=actor, cfg=cfg)
        if moved:
            result[wi] = moved
    return result
