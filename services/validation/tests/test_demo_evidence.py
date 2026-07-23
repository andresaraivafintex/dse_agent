"""WSE-E5-T11 — testes REAIS do vídeo @demo: Playwright de verdade (versão
pinada em services/validation/playwright/, browser Chromium instalado), página
fixture estática local (tests/fixtures/demos/wi_demo_fixture/ — o fixture
"oficial" do WS-C está em construção paralela; convenção demos/<work_item_id>/),
publicação REAL no Garage. Nenhum mock de Playwright/Garage/Postgres."""
from __future__ import annotations

from pathlib import Path

import httpx

from dse_contracts.activities import RunDemoEvidenceInput

from dse_validation import db
from dse_validation.evidence import garage
from dse_validation.evidence.demo import is_real_video, run_demo_evidence_core

FIXTURE_DEMO_DIR = Path(__file__).parent / "fixtures" / "demos" / "wi_demo_fixture"


def test_demo_evidence_records_real_video_and_trace(work_item_id, tenant_id, tmp_path):
    res = run_demo_evidence_core(
        RunDemoEvidenceInput(
            work_item_id=work_item_id,
            tenant_id=tenant_id,
            demo_dir=str(FIXTURE_DEMO_DIR),
            timeout_s=90,
        )
    )
    assert res.passed, res.detail
    assert res.video_artifact_key and res.video_artifact_key.startswith(f"{work_item_id}/demo_video/")
    assert res.trace_artifact_key and res.trace_artifact_key.startswith(f"{work_item_id}/playwright_trace/")
    assert res.duration_s > 0

    # o vídeo publicado no Garage é um webm REAL: baixa e valida header+tamanho
    url = garage.resolve_artifact_url(
        work_item_id=work_item_id, store_key=res.video_artifact_key, accessor="user:tester"
    )
    resp = httpx.get(url)
    assert resp.status_code == 200
    video_path = tmp_path / "video.webm"
    video_path.write_bytes(resp.content)
    assert video_path.stat().st_size > 0
    assert is_real_video(video_path), "header EBML/mp4 esperado — vídeo de verdade"

    # trace publicado também (zip não-vazio)
    trace_row = db.get_artifact(work_item_id, res.trace_artifact_key)
    assert trace_row is not None and trace_row["size_bytes"] > 0

    # audit (P8)
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM audit_log WHERE work_item_id = %s AND action = 'demo_evidence_run'",
                (work_item_id,),
            )
            assert cur.fetchone()[0] == 1
    finally:
        conn.close()


def test_demo_dir_missing_declines_cleanly(work_item_id, tenant_id):
    """P6 decline-never-truncate: sem diretório de demo => passed=False com
    detail explícito (nunca evidência 'fingida')."""
    res = run_demo_evidence_core(
        RunDemoEvidenceInput(
            work_item_id=work_item_id, tenant_id=tenant_id,
            demo_dir=f"/nonexistent/demos/{work_item_id}", timeout_s=30,
        )
    )
    assert res.passed is False
    assert "inexistente" in res.detail
    assert res.video_artifact_key is None


def test_demo_dir_without_demo_tagged_tests_declines(work_item_id, tenant_id, tmp_path):
    """Diretório existe mas nenhum teste tem a tag @demo => falha explícita."""
    demo_dir = tmp_path / "demos" / work_item_id
    demo_dir.mkdir(parents=True)
    (demo_dir / "other.spec.ts").write_text(
        "import { test, expect } from '@playwright/test';\n"
        "test('sem tag demo', async ({ page }) => { expect(1).toBe(1); });\n"
    )
    res = run_demo_evidence_core(
        RunDemoEvidenceInput(
            work_item_id=work_item_id, tenant_id=tenant_id,
            demo_dir=str(demo_dir), timeout_s=60,
        )
    )
    assert res.passed is False
    assert "nenhum teste @demo" in res.detail
