"""WSE-E5-T13 — visual diff REAL (Pillow pixel-diff, baseline no Garage).
Sem SaaS; imagens geradas com Pillow, comparação e round-trip do baseline
contra o artifact store de verdade."""
from __future__ import annotations

from pathlib import Path

import httpx

from dse_contracts.activities import RunVisualDiffInput

from dse_validation import db
from dse_validation.evidence import garage
from dse_validation.evidence.visual_diff import compare_images, run_visual_diff_core


def _png(path: Path, *, rect: tuple[int, int, int, int] | None = None,
         size=(320, 200), base_color=(240, 240, 240)) -> Path:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", size, base_color)
    draw = ImageDraw.Draw(img)
    draw.rectangle([10, 10, 100, 60], fill=(30, 90, 200))  # elemento estável
    if rect is not None:
        draw.rectangle(list(rect), fill=(200, 30, 30))  # a "mudança visual"
    img.save(path)
    return path


def test_first_run_creates_baseline_in_store(work_item_id, tenant_id, tmp_path):
    shot = _png(tmp_path / "candidate.png")
    res = run_visual_diff_core(
        RunVisualDiffInput(
            work_item_id=work_item_id, tenant_id=tenant_id,
            base_screenshot_key=None, candidate_screenshot_path=str(shot),
        )
    )
    assert res.baseline_created is True
    assert res.passed is True
    # a baseline está NO STORE (kind visual_baseline) e é recuperável
    assert res.diff_artifact_key.startswith(f"{work_item_id}/visual_baseline/")
    row = db.get_artifact(work_item_id, res.diff_artifact_key)
    assert row is not None and row["kind"] == "visual_baseline"


def test_identical_screenshot_passes_against_stored_baseline(work_item_id, tenant_id, tmp_path):
    shot = _png(tmp_path / "candidate.png")
    baseline = run_visual_diff_core(
        RunVisualDiffInput(
            work_item_id=work_item_id, tenant_id=tenant_id,
            base_screenshot_key=None, candidate_screenshot_path=str(shot),
        )
    )
    same = _png(tmp_path / "same.png")
    res = run_visual_diff_core(
        RunVisualDiffInput(
            work_item_id=work_item_id, tenant_id=tenant_id,
            base_screenshot_key=baseline.diff_artifact_key,
            candidate_screenshot_path=str(same), threshold_pct=0.1,
        )
    )
    assert res.baseline_created is False
    assert res.passed is True
    assert res.changed_pct == 0.0
    assert res.diff_artifact_key is None  # sem diff publicado quando passa


def test_visual_change_fails_and_publishes_diff_image(work_item_id, tenant_id, tmp_path):
    shot = _png(tmp_path / "candidate.png")
    baseline = run_visual_diff_core(
        RunVisualDiffInput(
            work_item_id=work_item_id, tenant_id=tenant_id,
            base_screenshot_key=None, candidate_screenshot_path=str(shot),
        )
    )
    changed = _png(tmp_path / "changed.png", rect=(120, 80, 300, 190))
    res = run_visual_diff_core(
        RunVisualDiffInput(
            work_item_id=work_item_id, tenant_id=tenant_id,
            base_screenshot_key=baseline.diff_artifact_key,
            candidate_screenshot_path=str(changed), threshold_pct=0.1,
        )
    )
    assert res.passed is False
    assert res.changed_pct > 0.1
    assert res.diff_artifact_key and res.diff_artifact_key.startswith(f"{work_item_id}/visual_diff/")
    # a imagem de diff publicada é um PNG real e recuperável do Garage
    url = garage.resolve_artifact_url(
        work_item_id=work_item_id, store_key=res.diff_artifact_key, accessor="user:tester"
    )
    resp = httpx.get(url)
    assert resp.status_code == 200
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_different_sizes_is_structural_change_100pct(tmp_path):
    from PIL import Image

    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    Image.new("RGB", (100, 100), (255, 255, 255)).save(a)
    Image.new("RGB", (200, 100), (255, 255, 255)).save(b)
    assert compare_images(a, b, tmp_path / "diff.png") == 100.0


def test_encoding_noise_within_tolerance_passes(tmp_path):
    """Tolerância por canal absorve ruído de compressão (anti-flake documentado)."""
    from PIL import Image

    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    Image.new("RGB", (100, 100), (100, 100, 100)).save(a)
    Image.new("RGB", (100, 100), (104, 104, 104)).save(b)  # delta 4 < tolerância 8
    assert compare_images(a, b, tmp_path / "diff.png") == 0.0
