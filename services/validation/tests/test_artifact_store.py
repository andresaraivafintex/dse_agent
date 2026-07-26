"""WSE-E5-T12 — REAL tests for the Garage artifact store (self-hosted S3 on
localhost:3900, docker-compose.wse.yml). NOTHING here mocks Garage, Postgres or
boto3 — the point of the test is the policy guarantee (expiration, quarantine,
access log) against the real store (P8).

Requires: `docker compose -f docker-compose.wse.yml up -d garage` + migration
0017_wse3.sql applied + ffmpeg on the host (multipart with a real >5MB video,
required by the revised ADR-18).
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from dse_contracts.activities import PublishArtifactInput

from dse_validation import db
from dse_validation.config import GarageConfig
from dse_validation.evidence import garage


def _audit_rows(work_item_id: str, action: str) -> list[dict]:
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT actor, action, details FROM audit_log WHERE work_item_id = %s AND action = %s",
                (work_item_id, action),
            )
            return [{"actor": r[0], "action": r[1], "details": r[2]} for r in cur.fetchall()]
    finally:
        conn.close()


@pytest.fixture(scope="module")
def garage_ready() -> GarageConfig:
    cfg = GarageConfig()
    garage.ensure_garage_ready(cfg)
    return cfg


@pytest.fixture
def small_file(tmp_path: Path) -> Path:
    p = tmp_path / "evidence.txt"
    p.write_text("evidence line " * 10)
    return p


def test_publish_and_presigned_get_roundtrip(garage_ready, small_file, work_item_id, tenant_id):
    ref = garage.publish_artifact_core(
        PublishArtifactInput(
            work_item_id=work_item_id, tenant_id=tenant_id, kind="test_report",
            local_path=str(small_file), content_type="text/plain", ttl_seconds=120,
        )
    )
    assert ref.store_key == f"{work_item_id}/test_report/{small_file.name}"
    assert ref.store_key.startswith(work_item_id)  # per-WorkItem prefix
    # PER-TENANT bucket (NFR-03)
    row = db.get_artifact(work_item_id, ref.store_key)
    assert row is not None and row["bucket"] == garage.bucket_for_tenant(tenant_id)
    assert row["multipart"] is False
    # real download via presigned URL
    resp = httpx.get(ref.presigned_url)
    assert resp.status_code == 200
    assert resp.text.startswith("evidence line")
    # audit (P8)
    assert len(_audit_rows(work_item_id, "artifact_published")) == 1


def test_presigned_url_expires_and_is_denied(garage_ready, small_file, work_item_id, tenant_id):
    """Phase 3 exit criterion: evidence links EXPIRE by policy — an expired URL
    returns DENIED (4xx, expired signature), proven against the real Garage."""
    ref = garage.publish_artifact_core(
        PublishArtifactInput(
            work_item_id=work_item_id, tenant_id=tenant_id, kind="test_report",
            local_path=str(small_file), content_type="text/plain", ttl_seconds=1,
        )
    )
    assert httpx.get(ref.presigned_url).status_code == 200  # valid right now
    time.sleep(3)
    resp = httpx.get(ref.presigned_url)
    # Garage denies an expired signature with 400 (AuthorizationHeaderMalformed/
    # expired); AWS S3 would use 403 — both mean DENIED, never the content.
    assert resp.status_code in (400, 401, 403), f"an expired URL should be denied, got {resp.status_code}"
    assert b"evidence line" not in resp.content
    # and policy-based resolution refuses it too (P6 — clean failure)
    with pytest.raises(PermissionError, match="expired"):
        garage.resolve_artifact_url(
            work_item_id=work_item_id, store_key=ref.store_key, accessor="user:tester"
        )


@pytest.fixture(scope="module")
def real_video_over_5mb(tmp_path_factory) -> Path:
    """REAL mp4 video (>5MB) generated with ffmpeg — required by the revised
    ADR-18 (validate multipart with a real video artifact, not a synthetic blob)."""
    out = tmp_path_factory.mktemp("video") / "demo_big.mp4"
    # plain testsrc compresses too well (ends up <1MB); temporal noise makes the
    # video realistically incompressible, and minrate forces the encoder to hold
    # the bitrate.
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=duration=10:size=1280x720:rate=30",
         "-vf", "noise=alls=40:allf=t+u",
         "-pix_fmt", "yuv420p",
         "-b:v", "8M", "-minrate", "8M", "-maxrate", "8M", "-bufsize", "16M",
         str(out)],
        check=True, timeout=180,
    )
    assert out.stat().st_size > 5 * 1024 * 1024, "the fixture must be >5MB"
    return out


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="requires ffmpeg (real video >5MB)")
def test_multipart_upload_with_real_video(garage_ready, real_video_over_5mb, work_item_id, tenant_id):
    ref = garage.publish_artifact_core(
        PublishArtifactInput(
            work_item_id=work_item_id, tenant_id=tenant_id, kind="demo_video",
            local_path=str(real_video_over_5mb), content_type="video/mp4", ttl_seconds=300,
        )
    )
    row = db.get_artifact(work_item_id, ref.store_key)
    assert row["multipart"] is True, "a >5MB upload must use real multipart"
    assert row["size_bytes"] == real_video_over_5mb.stat().st_size
    # the object reassembled in Garage is BYTE-IDENTICAL to the original video
    resp = httpx.get(ref.presigned_url)
    assert resp.status_code == 200
    assert len(resp.content) == real_video_over_5mb.stat().st_size
    assert resp.content[4:8] == b"ftyp"  # real mp4 header
    assert resp.content == real_video_over_5mb.read_bytes()


def test_access_log_associates_resolution_to_pr(garage_ready, small_file, work_item_id, tenant_id):
    """Every link resolution/open records an access attributable to the PR —
    input to the evidence-consumption metric."""
    ref = garage.publish_artifact_core(
        PublishArtifactInput(
            work_item_id=work_item_id, tenant_id=tenant_id, kind="test_report",
            local_path=str(small_file), content_type="text/plain", ttl_seconds=300,
        )
    )
    url1 = garage.resolve_artifact_url(
        work_item_id=work_item_id, store_key=ref.store_key,
        accessor="user:andre", pr_number=77, via="presign",
    )
    garage.resolve_artifact_url(
        work_item_id=work_item_id, store_key=ref.store_key,
        accessor="system:validation", pr_number=77, via="tracking_comment",
    )
    assert httpx.get(url1).status_code == 200
    accesses = db.list_artifact_accesses(work_item_id)
    assert len(accesses) == 2
    assert all(a["pr_number"] == 77 for a in accesses)
    assert {a["accessor"] for a in accesses} == {"user:andre", "system:validation"}
    assert {a["via"] for a in accesses} == {"presign", "tracking_comment"}
    assert len(_audit_rows(work_item_id, "artifact_link_resolved")) == 2


def test_quarantine_invalidates_access_before_ttl(garage_ready, small_file, work_item_id, tenant_id):
    """WS-F acceptance (EXISTING Phase 2 seam): a work item quarantined via
    `dse_platform.kill_switches.quarantine_work_item` => artifact moved to the
    quarantine prefix AND access invalidated BEFORE the TTL (which is 1h here)."""
    from dse_platform.kill_switches import is_quarantined, quarantine_work_item

    ref = garage.publish_artifact_core(
        PublishArtifactInput(
            work_item_id=work_item_id, tenant_id=tenant_id, kind="test_report",
            local_path=str(small_file), content_type="text/plain", ttl_seconds=3600,
        )
    )
    old_url = ref.presigned_url
    assert httpx.get(old_url).status_code == 200

    # REAL WS-F quarantine (dse_work_item_quarantine table + its own audit)
    quarantine_work_item(work_item_id, tenant_id, reason="suspected exfiltration", actor="user:operator")
    assert is_quarantined(work_item_id)

    moved = garage.sweep_quarantined_work_items()
    assert moved.get(work_item_id) == [ref.store_key]

    # 1) the OLD presigned URL (1h TTL still valid) is now denied —
    #    the original key no longer exists in the bucket.
    resp = httpx.get(old_url)
    assert resp.status_code in (403, 404), f"access should have been invalidated, got {resp.status_code}"
    # 2) policy-based resolution refuses explicitly (P6)
    with pytest.raises(PermissionError, match="quarantine"):
        garage.resolve_artifact_url(
            work_item_id=work_item_id, store_key=ref.store_key, accessor="user:andre"
        )
    # 3) the object was MOVED (not deleted — evidence preserved for auditing)
    row = db.get_artifact(work_item_id, ref.store_key)
    assert row["quarantined_at"] is not None
    assert row["quarantine_key"] == f"quarantine/{ref.store_key}"
    client = garage.s3_client(garage_ready)
    head = client.head_object(Bucket=row["bucket"], Key=row["quarantine_key"])
    assert head["ContentLength"] == small_file.stat().st_size
    # 4) audit (P8) — ours + WS-F's
    assert len(_audit_rows(work_item_id, "artifact_quarantined")) == 1
    assert len(_audit_rows(work_item_id, "work_item_quarantined")) == 1
    # 5) idempotent: a second sweep does not move it again
    assert garage.sweep_quarantined_work_items().get(work_item_id) is None
