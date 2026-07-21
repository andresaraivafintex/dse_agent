"""WSE-E5-T13 — visual diff self-hosted (Pillow pixel-diff com threshold).
Implementa a Activity `run_visual_diff` do contrato (`ACTIVITY_RUN_VISUAL_DIFF`,
input `RunVisualDiffInput`, retorno `VisualDiffResult`).

Decisão (P5 cheapest-first / P7 boring-first, documentada): comparação de
pixels com Pillow, 100% local — NENHUM SaaS de visual review. Uma ferramenta
dedicada (Argos/Percy/Chromatic ou o próprio `toHaveScreenshot` do Playwright
com baseline git) é o UPGRADE PATH quando a heurística de pixel puro começar a
dar falso-positivo com anti-aliasing/fonts — a fronteira é só esta função.

Baseline vive no ARTIFACT STORE (Garage, WSE-E5-T12), kind `visual_baseline`:
  - primeiro run (base_screenshot_key=None) => publica o candidato como
    baseline e retorna `baseline_created=True` (passed=True — não há com o
    que comparar; comparação começa no run seguinte).
  - runs seguintes => baixa a baseline do Garage, compara pixel a pixel
    (tolerância por canal para ruído de encoding), gera uma imagem de diff
    (pixels mudados em vermelho sobre o candidato esmaecido) e a publica como
    kind `visual_diff` quando o threshold é excedido.
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from dse_contracts.activities import PublishArtifactInput, RunVisualDiffInput, VisualDiffResult

from dse_validation import db
from dse_validation.config import GarageConfig
from dse_validation.evidence.garage import bucket_for_tenant, publish_artifact_core, s3_client

try:
    from dse_audit import emit as audit_emit
except ImportError:  # pragma: no cover
    audit_emit = None

logger = logging.getLogger("dse_validation.evidence.visual_diff")

# tolerância por canal (0-255) — absorve ruído de compressão sem mascarar
# mudança visual real; determinístico e documentado.
CHANNEL_TOLERANCE = 8
BASELINE_TTL_SECONDS = 30 * 24 * 3600  # baseline dura mais que evidência efêmera


def compare_images(baseline_path: str | Path, candidate_path: str | Path,
                   diff_out_path: str | Path) -> float:
    """Retorna o % de pixels mudados (0-100) e escreve a imagem de diff.
    Tamanhos diferentes => 100% (mudança estrutural, nunca 'adivinha')."""
    from PIL import Image, ImageChops

    base = Image.open(baseline_path).convert("RGB")
    cand = Image.open(candidate_path).convert("RGB")
    if base.size != cand.size:
        cand.save(diff_out_path)
        return 100.0

    diff = ImageChops.difference(base, cand)
    # máscara: pixel mudado se QUALQUER canal excede a tolerância
    gray = diff.convert("L")
    mask = gray.point(lambda v: 255 if v > CHANNEL_TOLERANCE else 0)
    changed = mask.histogram()[255]  # nº de pixels marcados (mask é L 0/255)
    total = mask.size[0] * mask.size[1]
    changed_pct = (changed / total) * 100.0 if total else 0.0

    # diff visual: candidato esmaecido + pixels mudados em vermelho
    faded = Image.blend(cand, Image.new("RGB", cand.size, (255, 255, 255)), 0.6)
    red = Image.new("RGB", cand.size, (220, 20, 20))
    overlay = Image.composite(red, faded, mask)
    overlay.save(diff_out_path)
    return changed_pct


def run_visual_diff_core(
    inp: RunVisualDiffInput,
    *,
    cfg: GarageConfig | None = None,
    actor: str = "system:validation",
) -> VisualDiffResult:
    cfg = cfg or GarageConfig()

    # primeiro run: cria a baseline no artifact store e retorna.
    if inp.base_screenshot_key is None:
        ref = publish_artifact_core(
            PublishArtifactInput(
                work_item_id=inp.work_item_id,
                tenant_id=inp.tenant_id,
                kind="visual_baseline",
                local_path=inp.candidate_screenshot_path,
                content_type="image/png",
                ttl_seconds=BASELINE_TTL_SECONDS,
            ),
            cfg=cfg,
            actor=actor,
        )
        if audit_emit is not None:
            audit_emit(
                actor=actor, action="visual_baseline_created", tenant_id=inp.tenant_id,
                work_item_id=inp.work_item_id, details={"store_key": ref.store_key},
            )
        return VisualDiffResult(
            work_item_id=inp.work_item_id, passed=True, changed_pct=0.0,
            diff_artifact_key=ref.store_key, baseline_created=True,
        )

    # runs seguintes: baixa a baseline do Garage e compara de verdade.
    bucket = bucket_for_tenant(inp.tenant_id, cfg)
    client = s3_client(cfg)
    with tempfile.TemporaryDirectory(prefix="dse-visual-diff-") as tmp:
        baseline_path = Path(tmp) / "baseline.png"
        diff_path = Path(tmp) / "diff.png"
        client.download_file(bucket, inp.base_screenshot_key, str(baseline_path))
        changed_pct = compare_images(baseline_path, inp.candidate_screenshot_path, diff_path)
        passed = changed_pct <= inp.threshold_pct

        diff_key = None
        if not passed:
            ref = publish_artifact_core(
                PublishArtifactInput(
                    work_item_id=inp.work_item_id,
                    tenant_id=inp.tenant_id,
                    kind="visual_diff",
                    local_path=str(diff_path),
                    content_type="image/png",
                ),
                cfg=cfg,
                actor=actor,
            )
            diff_key = ref.store_key

    if audit_emit is not None:
        audit_emit(
            actor=actor, action="visual_diff_run", tenant_id=inp.tenant_id,
            work_item_id=inp.work_item_id,
            details={
                "baseline_key": inp.base_screenshot_key,
                "changed_pct": round(changed_pct, 4),
                "threshold_pct": inp.threshold_pct,
                "passed": passed,
                "diff_artifact_key": diff_key,
            },
        )
    return VisualDiffResult(
        work_item_id=inp.work_item_id, passed=passed,
        changed_pct=round(changed_pct, 4), diff_artifact_key=diff_key,
        baseline_created=False,
    )
