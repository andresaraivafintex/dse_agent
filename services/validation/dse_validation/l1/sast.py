"""WSE-E1-T2 (parte 1) — SAST via `bandit` (self-hostable, real, sem custo de
licença) rodando dentro do sandbox contra o diretório do diff. Normaliza a
saída JSON do bandit para `L1Finding` (mesmo formato dos outros checks)."""
from __future__ import annotations

import json

from dse_contracts import L1Finding

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
        return L1Finding(check="sast", passed=False, detail=f"bandit não encontrado: {result.stderr.strip()}")

    # bandit sai com returncode 1 quando encontra issues (não é erro de execução).
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return L1Finding(
            check="sast",
            passed=False,
            detail=f"bandit não produziu JSON válido (exit={result.returncode}): {result.stderr[:2000]}",
        )

    findings = payload.get("results", [])
    gating = [
        f for f in findings if _SEVERITY_ORDER.get(f.get("issue_severity", "LOW"), 1) >= gate
    ]

    if not gating:
        detail = f"{len(findings)} achado(s) de SAST no total, nenhum >= {severity_gate}"
        return L1Finding(check="sast", passed=True, detail=detail)

    lines = [
        f"- [{f.get('issue_severity')}] {f.get('test_id')} {f.get('filename')}:{f.get('line_number')} — {f.get('issue_text')}"
        for f in gating[:20]
    ]
    detail = f"{len(gating)} achado(s) de SAST >= {severity_gate}:\n" + "\n".join(lines)
    return L1Finding(check="sast", passed=False, detail=detail)
