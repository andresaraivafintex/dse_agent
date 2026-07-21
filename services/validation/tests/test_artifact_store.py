"""WSE-E5-T12 — testes REAIS do artifact store Garage (S3 self-hosted em
localhost:3900, docker-compose.wse.yml). NADA aqui mocka o Garage, o Postgres
ou o boto3 — o ponto do teste é a garantia de política (expiração, quarentena,
log de acesso) contra o store de verdade (P8).

Requer: `docker compose -f docker-compose.wse.yml up -d garage` + migração
0017_wse3.sql aplicada + ffmpeg no host (multipart com vídeo real >5MB,
obrigação do ADR-18 revisado).
"""
from __future__ import annotations

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
    p.write_text("linha de evidencia " * 10)
    return p


def test_publish_and_presigned_get_roundtrip(garage_ready, small_file, work_item_id, tenant_id):
    ref = garage.publish_artifact_core(
        PublishArtifactInput(
            work_item_id=work_item_id, tenant_id=tenant_id, kind="test_report",
            local_path=str(small_file), content_type="text/plain", ttl_seconds=120,
        )
    )
    assert ref.store_key == f"{work_item_id}/test_report/{small_file.name}"
    assert ref.store_key.startswith(work_item_id)  # prefixo por WorkItem
    # bucket POR TENANT (NFR-03)
    row = db.get_artifact(work_item_id, ref.store_key)
    assert row is not None and row["bucket"] == garage.bucket_for_tenant(tenant_id)
    assert row["multipart"] is False
    # download real via presigned URL
    resp = httpx.get(ref.presigned_url)
    assert resp.status_code == 200
    assert resp.text.startswith("linha de evidencia")
    # audit (P8)
    assert len(_audit_rows(work_item_id, "artifact_published")) == 1


def test_presigned_url_expires_and_is_denied(garage_ready, small_file, work_item_id, tenant_id):
    """Exit da Fase 3: links de evidência EXPIRAM por política — URL expirada
    retorna NEGADO (4xx assinatura expirada), provado contra o Garage real."""
    ref = garage.publish_artifact_core(
        PublishArtifactInput(
            work_item_id=work_item_id, tenant_id=tenant_id, kind="test_report",
            local_path=str(small_file), content_type="text/plain", ttl_seconds=1,
        )
    )
    assert httpx.get(ref.presigned_url).status_code == 200  # válida agora
    time.sleep(3)
    resp = httpx.get(ref.presigned_url)
    # Garage nega assinatura expirada com 400 (AuthorizationHeaderMalformed/
    # expired); AWS S3 usaria 403 — ambos são NEGADO, nunca o conteúdo.
    assert resp.status_code in (400, 401, 403), f"URL expirada deveria ser negada, veio {resp.status_code}"
    assert b"linha de evidencia" not in resp.content
    # e a resolução por política também recusa (P6 — falha limpa)
    with pytest.raises(PermissionError, match="expirado"):
        garage.resolve_artifact_url(
            work_item_id=work_item_id, store_key=ref.store_key, accessor="user:tester"
        )


@pytest.fixture(scope="module")
def real_video_over_5mb(tmp_path_factory) -> Path:
    """Vídeo mp4 REAL (>5MB) gerado com ffmpeg — obrigação do ADR-18 revisado
    (validar multipart com artefato de vídeo real, não um blob sintético)."""
    out = tmp_path_factory.mktemp("video") / "demo_big.mp4"
    # testsrc puro comprime demais (fica <1MB); ruído temporal torna o vídeo
    # realisticamente incompressível, e minrate força o encoder a manter bitrate.
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=duration=10:size=1280x720:rate=30",
         "-vf", "noise=alls=40:allf=t+u",
         "-pix_fmt", "yuv420p",
         "-b:v", "8M", "-minrate", "8M", "-maxrate", "8M", "-bufsize", "16M",
         str(out)],
        check=True, timeout=180,
    )
    assert out.stat().st_size > 5 * 1024 * 1024, "fixture precisa ter >5MB"
    return out


def test_multipart_upload_with_real_video(garage_ready, real_video_over_5mb, work_item_id, tenant_id):
    ref = garage.publish_artifact_core(
        PublishArtifactInput(
            work_item_id=work_item_id, tenant_id=tenant_id, kind="demo_video",
            local_path=str(real_video_over_5mb), content_type="video/mp4", ttl_seconds=300,
        )
    )
    row = db.get_artifact(work_item_id, ref.store_key)
    assert row["multipart"] is True, "upload >5MB deve usar multipart real"
    assert row["size_bytes"] == real_video_over_5mb.stat().st_size
    # o objeto remontado no Garage é BYTE-IDÊNTICO ao vídeo original
    resp = httpx.get(ref.presigned_url)
    assert resp.status_code == 200
    assert len(resp.content) == real_video_over_5mb.stat().st_size
    assert resp.content[4:8] == b"ftyp"  # header mp4 real
    assert resp.content == real_video_over_5mb.read_bytes()


def test_access_log_associates_resolution_to_pr(garage_ready, small_file, work_item_id, tenant_id):
    """Cada resolução/abertura de link registra acesso associável ao PR —
    insumo da métrica evidence consumption."""
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
    """Aceite do WS-F (costura EXISTENTE da Fase 2): work item quarantinado via
    `dse_platform.kill_switches.quarantine_work_item` => artefato movido para o
    prefixo de quarentena E acesso invalidado ANTES do TTL (que aqui é 1h)."""
    from dse_platform.kill_switches import is_quarantined, quarantine_work_item

    ref = garage.publish_artifact_core(
        PublishArtifactInput(
            work_item_id=work_item_id, tenant_id=tenant_id, kind="test_report",
            local_path=str(small_file), content_type="text/plain", ttl_seconds=3600,
        )
    )
    old_url = ref.presigned_url
    assert httpx.get(old_url).status_code == 200

    # quarentena REAL do WS-F (tabela dse_work_item_quarantine + audit dele)
    quarantine_work_item(work_item_id, tenant_id, reason="suspeita de exfiltração", actor="user:operador")
    assert is_quarantined(work_item_id)

    moved = garage.sweep_quarantined_work_items()
    assert moved.get(work_item_id) == [ref.store_key]

    # 1) a URL presigned ANTIGA (TTL de 1h ainda vigente) agora é negada —
    #    a chave original não existe mais no bucket.
    resp = httpx.get(old_url)
    assert resp.status_code in (403, 404), f"acesso deveria estar invalidado, veio {resp.status_code}"
    # 2) resolução por política recusa explicitamente (P6)
    with pytest.raises(PermissionError, match="quarentena"):
        garage.resolve_artifact_url(
            work_item_id=work_item_id, store_key=ref.store_key, accessor="user:andre"
        )
    # 3) o objeto foi MOVIDO (não deletado — evidência preservada p/ auditoria)
    row = db.get_artifact(work_item_id, ref.store_key)
    assert row["quarantined_at"] is not None
    assert row["quarantine_key"] == f"quarantine/{ref.store_key}"
    client = garage.s3_client(garage_ready)
    head = client.head_object(Bucket=row["bucket"], Key=row["quarantine_key"])
    assert head["ContentLength"] == small_file.stat().st_size
    # 4) audit (P8) — nosso + o do WS-F
    assert len(_audit_rows(work_item_id, "artifact_quarantined")) == 1
    assert len(_audit_rows(work_item_id, "work_item_quarantined")) == 1
    # 5) idempotente: segunda varredura não move de novo
    assert garage.sweep_quarantined_work_items().get(work_item_id) is None
