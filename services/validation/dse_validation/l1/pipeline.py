"""WSE-E1 — junta os 3 sub-tarefas (T1 lint/typecheck/test/build, T2 SAST +
secret-scan, T3 diff vs PlanArtifact) num `L1Result` único.

Falha em qualquer check => `L1Result.passed=False`. Fase 1 não tem L2 —
falha aqui volta ao Coder por decisão do workflow do WS-B; este módulo só
reporta pass/fail com evidência, nunca decide o que fazer a seguir (P1)."""
from __future__ import annotations

from dse_contracts import GateStatus, L1Finding, L1Result, PlanArtifact

from dse_validation import db
from dse_validation.config import L1Config
from dse_validation.l1 import plan_compliance, quality_checks, sast, secret_scan
from dse_validation.sandbox_exec import SandboxExecutor

try:
    from dse_audit import emit as audit_emit
except ImportError:  # pragma: no cover - defensive, dse_audit é sempre instalado no venv-wse
    audit_emit = None


def run_l1_pipeline_core(
    executor: SandboxExecutor,
    work_item_id: str,
    tenant_id: str,
    plan: PlanArtifact,
    base_sha: str,
    head_sha: str,
    target_dir: str = ".",
    cfg: L1Config | None = None,
    actor: str = "system:validation",
    persist: bool = True,
) -> L1Result:
    cfg = cfg or L1Config.from_trusted_manifest(executor, base_sha)

    findings: list[L1Finding] = []
    if cfg.manifest_status != GateStatus.PASS:
        findings.append(
            L1Finding(
                check="l1_manifest",
                passed=False,
                status=cfg.manifest_status,
                detail=cfg.manifest_detail,
            )
        )
    findings.extend([
        quality_checks.lint_check(executor, cfg),
        quality_checks.typecheck_check(executor, cfg),
        quality_checks.test_check(executor, cfg),
        quality_checks.build_check(executor, cfg),
        sast.sast_check(executor, target_dir, cfg.sast_severity_gate),
        secret_scan.secret_scan_check(executor, target_dir),
    ])
    findings.extend(
        plan_compliance.plan_compliance_findings(executor, plan, base_sha, head_sha)
    )

    passed = all(f.passed for f in findings)
    result = L1Result(
        work_item_id=work_item_id,
        passed=passed,
        findings=findings,
        base_sha=base_sha,
        head_sha=head_sha,
    )

    if persist:
        db.record_validation_run(
            work_item_id, tenant_id, passed, [f.model_dump() for f in findings]
        )
    if audit_emit is not None:
        audit_emit(
            actor=actor,
            action="l1_pipeline_run",
            tenant_id=tenant_id,
            work_item_id=work_item_id,
            details={
                "passed": passed,
                "status": result.status.value if result.status else None,
                "base_sha": base_sha,
                "head_sha": head_sha,
                "checks": {
                    f.check: f.status.value if f.status else None for f in findings
                },
            },
        )
    return result
