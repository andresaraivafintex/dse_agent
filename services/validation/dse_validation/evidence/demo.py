"""WSE-E5-T11 — vídeo @demo Playwright. Implementa a Activity
`run_demo_evidence` do contrato (`ACTIVITY_RUN_DEMO_EVIDENCE`, input
`RunDemoEvidenceInput`, retorno `DemoEvidenceResult`).

O TESTE @demo é autorado pelo Tester (WS-C, convenção `demos/<work_item_id>/`,
ADR-27); a EXECUÇÃO aqui é 100% determinística (P1): `npx playwright test
--grep @demo` com `video: 'on'` + `trace: 'on'`, Playwright REAL (versão pinada
em services/validation/playwright/package.json, browsers instalados de
verdade). O vídeo (.webm) e o trace (.zip) resultantes são publicados no
artifact store Garage via `publish_artifact_core` (WSE-E5-T12) e as chaves
voltam no `DemoEvidenceResult`.

Verificação de que o vídeo é REAL (não um arquivo vazio): tamanho > 0 e header
EBML (`\\x1a\\x45\\xdf\\xa3` — container webm/matroska que o Playwright grava).
P6: nenhum resultado é "meio verdade" — sem teste @demo => passed=False com
detail explícito; timeout => passed=False com detail explícito.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path

from dse_contracts.activities import DemoEvidenceResult, PublishArtifactInput, RunDemoEvidenceInput

from dse_validation.evidence.garage import publish_artifact_core

try:
    from dse_audit import emit as audit_emit
except ImportError:  # pragma: no cover
    audit_emit = None

logger = logging.getLogger("dse_validation.evidence.demo")

_EBML_HEADER = b"\x1a\x45\xdf\xa3"  # webm/matroska
_MP4_FTYp = b"ftyp"  # mp4 (offset 4)

RUNNER_DIR = Path(__file__).resolve().parent.parent.parent / "playwright"

_CONFIG_TEMPLATE = """\
// gerado por dse_validation.evidence.demo — não editar
module.exports = {{
  testDir: {test_dir!r},
  timeout: {timeout_ms},
  retries: 0,
  workers: 1,
  reporter: [['json', {{ outputFile: {report_file!r} }}]],
  outputDir: {output_dir!r},
  use: {{
    video: 'on',
    trace: 'on',
    {base_url_line}
  }},
}};
"""


def is_real_video(path: str | Path) -> bool:
    """Tamanho > 0 e header de container de vídeo (webm EBML ou mp4 ftyp)."""
    p = Path(path)
    if not p.is_file() or p.stat().st_size == 0:
        return False
    head = p.read_bytes()[:12]
    return head.startswith(_EBML_HEADER) or head[4:8] == _MP4_FTYp


def _find_outputs(output_dir: Path) -> tuple[Path | None, Path | None]:
    videos = sorted(output_dir.rglob("*.webm")) + sorted(output_dir.rglob("*.mp4"))
    traces = sorted(output_dir.rglob("trace.zip"))
    return (videos[0] if videos else None, traces[0] if traces else None)


def run_demo_evidence_core(
    inp: RunDemoEvidenceInput,
    *,
    runner_dir: Path | None = None,
    publish=publish_artifact_core,
    actor: str = "system:validation",
) -> DemoEvidenceResult:
    runner_dir = runner_dir or RUNNER_DIR
    demo_dir = inp.demo_dir or f"demos/{inp.work_item_id}/"
    demo_path = Path(demo_dir).resolve()
    started = time.monotonic()

    if not demo_path.is_dir():
        return DemoEvidenceResult(
            work_item_id=inp.work_item_id,
            passed=False,
            detail=f"diretório de demo inexistente: {demo_path} (convenção demos/<work_item_id>/)",
        )

    with tempfile.TemporaryDirectory(prefix=f"dse-demo-{inp.work_item_id}-") as tmp:
        output_dir = Path(tmp) / "test-results"
        report_file = Path(tmp) / "report.json"
        config_path = Path(tmp) / "playwright.config.js"
        base_url_line = f"baseURL: {inp.base_url!r}," if inp.base_url else ""
        config_path.write_text(
            _CONFIG_TEMPLATE.format(
                test_dir=str(demo_path),
                timeout_ms=inp.timeout_s * 1000,
                report_file=str(report_file),
                output_dir=str(output_dir),
                base_url_line=base_url_line,
            )
        )

        env = dict(os.environ)
        # o diretório de demos vive no repo ALVO (fora do projeto node do
        # runner) — NODE_PATH permite ao spec resolver @playwright/test a
        # partir do node_modules pinado do runner.
        env["NODE_PATH"] = str(Path(runner_dir) / "node_modules")
        if inp.base_url:
            env["DSE_DEMO_BASE_URL"] = inp.base_url
        try:
            proc = subprocess.run(
                ["npx", "playwright", "test", "--grep", "@demo", "--config", str(config_path)],
                cwd=str(runner_dir),
                capture_output=True,
                text=True,
                timeout=inp.timeout_s + 60,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return DemoEvidenceResult(
                work_item_id=inp.work_item_id,
                passed=False,
                duration_s=time.monotonic() - started,
                detail=f"playwright excedeu o timeout de {inp.timeout_s + 60}s (P6: falha limpa)",
            )

        # relatório JSON estruturado (não só exit code)
        n_tests = 0
        if report_file.is_file():
            try:
                report = json.loads(report_file.read_text())
                n_tests = sum(
                    len(spec.get("tests", []))
                    for suite in report.get("suites", [])
                    for spec in _walk_specs(suite)
                )
            except (json.JSONDecodeError, KeyError):  # pragma: no cover
                pass
        if n_tests == 0:
            return DemoEvidenceResult(
                work_item_id=inp.work_item_id,
                passed=False,
                duration_s=time.monotonic() - started,
                detail=(
                    f"nenhum teste @demo encontrado em {demo_path} "
                    f"(P6 decline-never-truncate — não fingimos evidência). "
                    f"stdout: {proc.stdout[-400:]}"
                ),
            )

        passed = proc.returncode == 0
        video_path, trace_path = _find_outputs(output_dir)

        video_key = trace_key = None
        detail_parts: list[str] = []
        if video_path is not None and is_real_video(video_path):
            ref = publish(
                PublishArtifactInput(
                    work_item_id=inp.work_item_id,
                    tenant_id=inp.tenant_id,
                    kind="demo_video",
                    local_path=str(video_path),
                    content_type="video/webm" if video_path.suffix == ".webm" else "video/mp4",
                )
            )
            video_key = ref.store_key
        else:
            detail_parts.append("vídeo ausente ou inválido (header/tamanho)")

        if trace_path is not None and trace_path.stat().st_size > 0:
            ref = publish(
                PublishArtifactInput(
                    work_item_id=inp.work_item_id,
                    tenant_id=inp.tenant_id,
                    kind="playwright_trace",
                    local_path=str(trace_path),
                    content_type="application/zip",
                )
            )
            trace_key = ref.store_key
        else:
            detail_parts.append("trace ausente")

        if not passed:
            detail_parts.append(f"playwright exit={proc.returncode}: {proc.stdout[-400:]}")

        duration = time.monotonic() - started
        result = DemoEvidenceResult(
            work_item_id=inp.work_item_id,
            passed=passed and video_key is not None,
            video_artifact_key=video_key,
            trace_artifact_key=trace_key,
            duration_s=duration,
            detail="; ".join(detail_parts),
        )
        if audit_emit is not None:
            audit_emit(
                actor=actor,
                action="demo_evidence_run",
                tenant_id=inp.tenant_id,
                work_item_id=inp.work_item_id,
                details={
                    "passed": result.passed,
                    "n_tests": n_tests,
                    "video_artifact_key": video_key,
                    "trace_artifact_key": trace_key,
                    "duration_s": round(duration, 2),
                },
            )
        return result


def _walk_specs(suite: dict):
    for spec in suite.get("specs", []):
        yield spec
    for sub in suite.get("suites", []):
        yield from _walk_specs(sub)
