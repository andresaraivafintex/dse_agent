"""WSE-E1-T2 (part 1) — SAST via `bandit` (self-hostable, real, no license
cost) running inside the sandbox against the diff directory. Normalizes
bandit's JSON output into `L1Finding` (the same shape as the other checks)."""
from __future__ import annotations

import json

from dse_contracts import GateStatus, L1Finding

from dse_validation.config import default_scan_timeout_seconds
from dse_validation.sandbox_exec import SandboxExecutor

_SEVERITY_ORDER = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}


def sast_check(
    executor: SandboxExecutor,
    target_dir: str = ".",
    severity_gate: str = "MEDIUM",
    timeout: int | None = None,
) -> L1Finding:
    gate = _SEVERITY_ORDER.get(severity_gate.upper(), 2)
    # `None` — how the pipeline calls it — resolves the platform default instead
    # of the 120 s that used to be frozen in this signature, out of reach of both
    # the manifest and the environment. At the 20 files/s bandit measures inside
    # the sandbox pod, 120 s stops covering a repository at ~2.400 `.py`.
    if timeout is None:
        timeout = default_scan_timeout_seconds("sast")
    result = executor.run(["bandit", "-r", target_dir, "-f", "json", "-q"], timeout=timeout)

    if result.returncode == 127:
        return L1Finding(
            check="sast",
            passed=False,
            status=GateStatus.ERROR,
            detail=f"bandit not found: {result.stderr.strip()}",
            summary="bandit not found (exit 127)",
        )

    # A killed bandit prints NOTHING, and `json.loads("{}")` below yields zero
    # findings — i.e. a silent PASS on a security gate that never ran. It was
    # unreachable while 120 s was frozen in the signature; now that the budget
    # comes from the environment and the manifest, a short clock would BUY a
    # green SAST. Same shape as `secret_scan_check`: no verdict is an ERROR.
    if result.timed_out:
        return L1Finding(
            check="sast",
            passed=False,
            status=GateStatus.ERROR,
            detail=(
                f"bandit timed out after {timeout}s — no SAST verdict was produced "
                f"(scanning {target_dir}); raise the sast timeout or scan less"
            ),
            summary=(
                f"bandit timed out after {timeout}s — no SAST verdict was "
                "produced; raise the sast timeout or scan less"
            ),
        )

    # bandit exits with returncode 1 when it finds issues (not an execution error).
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return L1Finding(
            check="sast",
            passed=False,
            status=GateStatus.ERROR,
            detail=f"bandit did not produce valid JSON (exit={result.returncode}): {result.stderr[:2000]}",
            summary=f"bandit did not produce valid JSON (exit={result.returncode})",
        )

    findings = payload.get("results", [])
    gating = [
        f for f in findings if _SEVERITY_ORDER.get(f.get("issue_severity", "LOW"), 1) >= gate
    ]

    if not gating:
        detail = f"{len(findings)} SAST finding(s) in total, none >= {severity_gate}"
        return L1Finding(check="sast", passed=True, detail=detail, summary=detail)

    lines = [
        f"- [{f.get('issue_severity')}] {f.get('test_id')} {f.get('filename')}:{f.get('line_number')} — {f.get('issue_text')}"
        for f in gating[:20]
    ]
    summary = f"{len(gating)} SAST finding(s) >= {severity_gate}"
    # `lines` holds bandit's issue_text, and B105/B106/B107 render that as
    # "Possible hardcoded password: '<value>'" — the credential itself. It
    # stays in `detail`, which only reaches validation_runs.
    detail = summary + ":\n" + "\n".join(lines)
    return L1Finding(check="sast", passed=False, detail=detail, summary=summary)
