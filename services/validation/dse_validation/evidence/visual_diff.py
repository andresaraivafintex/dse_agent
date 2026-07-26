"""WSE-E5-T13 — self-hosted visual diff (Pillow pixel-diff with threshold).
Implements the contract's `run_visual_diff` Activity (`ACTIVITY_RUN_VISUAL_DIFF`,
input `RunVisualDiffInput`, returns `VisualDiffResult`).

Decision (P5 cheapest-first / P7 boring-first, documented): pixel comparison with
Pillow, 100% local — NO visual-review SaaS. A dedicated tool (Argos/Percy/
Chromatic, or Playwright's own `toHaveScreenshot` with a git baseline) is the
UPGRADE PATH once the pure-pixel heuristic starts producing false positives with
anti-aliasing/fonts — the boundary is just this function.

The baseline lives in the ARTIFACT STORE (Garage, WSE-E5-T12), kind
`visual_baseline`:
  - first run (base_screenshot_key=None) => publishes the candidate as the
    baseline and returns `baseline_created=True` (passed=True — there is nothing
    to compare against; comparison starts on the next run).
  - subsequent runs => downloads the baseline from Garage, compares pixel by
    pixel (per-channel tolerance for encoding noise), generates a diff image
    (changed pixels in red over the faded candidate) and publishes it as kind
    `visual_diff` when the threshold is exceeded.
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from dse_contracts.activities import PublishArtifactInput, RunVisualDiffInput, VisualDiffResult

from dse_validation.config import GarageConfig
from dse_validation.evidence.garage import bucket_for_tenant, publish_artifact_core, s3_client

try:
    from dse_audit import emit as audit_emit
except ImportError:  # pragma: no cover
    audit_emit = None

logger = logging.getLogger("dse_validation.evidence.visual_diff")

# per-channel tolerance (0-255) — absorbs compression noise without masking a
# real visual change; deterministic and documented.
CHANNEL_TOLERANCE = 8
BASELINE_TTL_SECONDS = 30 * 24 * 3600  # the baseline outlives ephemeral evidence


def compare_images(baseline_path: str | Path, candidate_path: str | Path,
                   diff_out_path: str | Path) -> float:
    """Returns the % of changed pixels (0-100) and writes the diff image.
    Different sizes => 100% (structural change, never a 'guess')."""
    from PIL import Image, ImageChops

    base = Image.open(baseline_path).convert("RGB")
    cand = Image.open(candidate_path).convert("RGB")
    if base.size != cand.size:
        cand.save(diff_out_path)
        return 100.0

    diff = ImageChops.difference(base, cand)
    # mask: a pixel is changed if ANY channel exceeds the tolerance
    gray = diff.convert("L")
    mask = gray.point(lambda v: 255 if v > CHANNEL_TOLERANCE else 0)
    changed = mask.histogram()[255]  # number of marked pixels (mask is L 0/255)
    total = mask.size[0] * mask.size[1]
    changed_pct = (changed / total) * 100.0 if total else 0.0

    # visual diff: faded candidate + changed pixels in red
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

    # first run: creates the baseline in the artifact store and returns.
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

    # subsequent runs: downloads the baseline from Garage and actually compares.
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
