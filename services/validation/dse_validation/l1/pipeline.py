"""WSE-E1 — joins the 3 sub-tasks (T1 lint/typecheck/test/build, T2 SAST +
secret-scan, T3 diff vs PlanArtifact) into a single `L1Result`.

Failure on any check => `L1Result.passed=False`. Phase 1 has no L2 — a failure
here goes back to the Coder by decision of the WS-B workflow; this module only
reports pass/fail with evidence, it never decides what to do next (P1)."""
from __future__ import annotations

from collections.abc import Callable

from dse_contracts import GateStatus, L1Finding, L1Result, PlanArtifact

from dse_validation import db
from dse_validation.config import L1Config
from dse_validation.l1 import plan_compliance, quality_checks, sast, secret_scan
from dse_validation.sandbox_exec import SandboxExecutor

try:
    from dse_audit import emit as audit_emit
except ImportError:  # pragma: no cover - defensive, dse_audit is always installed in venv-wse
    audit_emit = None


def _ignore_step(_name: str) -> None:
    """Default `on_step`: the core stays a plain synchronous function for the
    tests and for any caller that has nothing to report progress to."""


#: What an operator sees when a gate failed and its branch declared no summary.
#: It is deliberately useless: silence here means a NEW branch forgot to author
#: one, and the operator should read `validation_runs`, not be handed whatever
#: that branch happened to interpolate.
_NO_SUMMARY = "(the check reported no publishable reason — see validation_runs)"


def _audit_safe_summary(summary: str | None) -> str:
    """Bound and flatten a platform-authored summary for the ledger.

    `summary` is already value-free by construction (see `L1Finding.summary`);
    this only stops a malformed one from being unbounded or multi-line, because
    `audit_log` is append-only and a bad write can never be amended."""
    first = (summary or "").replace("\x00", "").splitlines()
    return first[0][:600] if first else ""


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
    on_step: Callable[[str], None] | None = None,
) -> L1Result:
    # `on_step` is called with the name of each stage BEFORE that stage runs.
    # This is the pipeline's only progress signal, and the Activity wrapper needs
    # it for two things it cannot get any other way (see `activities.py`):
    #   1. the Temporal heartbeat carries the stage that is ACTUALLY running, so
    #      an L1 that dies reads "test, 412s in" instead of "the Activity died";
    #   2. it is the only point where an already-cancelled Activity can stop the
    #      pipeline — `asyncio.to_thread` cannot interrupt this thread, so the
    #      wrapper's callback raises here rather than letting the remaining
    #      checks keep burning the sandbox's single vCPU.
    # Anything raised by `on_step` therefore propagates on purpose.
    step = on_step or _ignore_step

    step("l1_manifest")
    cfg = cfg or L1Config.from_trusted_manifest(executor, base_sha)

    findings: list[L1Finding] = []
    if cfg.manifest_status != GateStatus.PASS:
        findings.append(
            L1Finding(
                check="l1_manifest",
                passed=False,
                status=cfg.manifest_status,
                detail=cfg.manifest_detail,
                summary=cfg.manifest_summary,
            )
        )
    step("lint")
    findings.append(quality_checks.lint_check(executor, cfg))
    step("typecheck")
    findings.append(quality_checks.typecheck_check(executor, cfg))
    step("test")
    findings.append(quality_checks.test_check(executor, cfg))
    step("build")
    findings.append(quality_checks.build_check(executor, cfg))
    step("sast")
    findings.append(
        sast.sast_check(
            executor, target_dir, cfg.sast_severity_gate, cfg.timeout_for("sast")
        )
    )
    step("secret_scan")
    findings.append(
        secret_scan.secret_scan_check(executor, target_dir, cfg.timeout_for("secret_scan"))
    )
    step("plan_compliance")
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

    step("persist")
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
                # WHY, not just WHICH — from `summary` and never from `detail`.
                #
                # A gate reading ERROR with no reason anywhere is a gate nobody
                # can debug; diagnosing one cost two wrong guesses before anyone
                # noticed the reason was discarded at write time. But `detail`
                # carries what the gate SAW — scanner output, compiler output,
                # matched source lines — and audit_log is the one store that
                # cannot take that: append-only (REVOKE plus the 0028 trigger),
                # refused by retention.py by design, and copied verbatim into
                # console_rm.timeline_events.data by the projector. A value
                # written here can be rotated, never scrubbed.
                #
                # The first attempt published `detail`'s first line and skipped
                # the checks named "secret_scan" and "sast". That was wrong in
                # BOTH directions, which is why the rule now lives on the
                # branch: it silenced sast's ERROR reason — the very incident
                # this exists for — while still publishing lint's exit-127
                # branch, whose first line is raw stderr from a command the
                # customer's own manifest chose. A denylist of check names has
                # to predict every future branch that forgets to prepend a
                # summary; it had already missed four.
                #
                # `detail` keeps living in validation_runs, which is where
                # debugging happens and which retention can still clean.
                "failures": {
                    f.check: _audit_safe_summary(f.summary) or _NO_SUMMARY
                    for f in findings
                    if not f.passed
                },
            },
        )
    return result
