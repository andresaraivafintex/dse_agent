"""WSE-E1-T2 (part 1) — SAST via `bandit` (self-hostable, real, no license
cost) running inside the sandbox against the diff directory. Normalizes
bandit's JSON output into `L1Finding` (the same shape as the other checks)."""
from __future__ import annotations

import json

from dse_contracts import GateStatus, L1Finding

from dse_validation.sandbox_exec import SandboxExecutor

_SEVERITY_ORDER = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}


def sast_check(
    executor: SandboxExecutor,
    target_dir: str = ".",
    severity_gate: str = "MEDIUM",
    timeout: int = 120,
) -> L1Finding:
    gate = _SEVERITY_ORDER.get(severity_gate.upper(), 2)
    result = executor.run(["bandit", "-r", target_dir, "-f", "json", "-q"], timeout=timeout)

    if result.returncode == 127:
        return L1Finding(
            check="sast",
            passed=False,
            status=GateStatus.ERROR,
            detail=f"bandit not found: {result.stderr.strip()}",
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
        )

    findings = payload.get("results", [])
    gating = [
        f for f in findings if _SEVERITY_ORDER.get(f.get("issue_severity", "LOW"), 1) >= gate
    ]

    if not gating:
        detail = f"{len(findings)} SAST finding(s) in total, none >= {severity_gate}"
        return L1Finding(check="sast", passed=True, detail=detail)

    lines = [
        f"- [{f.get('issue_severity')}] {f.get('test_id')} {f.get('filename')}:{f.get('line_number')} — {f.get('issue_text')}"
        for f in gating[:20]
    ]
    detail = f"{len(gating)} SAST finding(s) >= {severity_gate}:\n" + "\n".join(lines)
    return L1Finding(check="sast", passed=False, detail=detail)
