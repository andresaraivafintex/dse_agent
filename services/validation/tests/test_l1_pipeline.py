"""WSE-E1 — integração dos 3 sub-tarefas (T1+T2+T3) num único `L1Result`.
Persiste em `validation_runs` (Postgres REAL) e emite audit (via dse_audit
real, Postgres REAL) — prova P8 (evidência) fim a fim."""
from __future__ import annotations

import psycopg2

from dse_contracts import GateStatus, PlanArtifact

from dse_validation.l1.pipeline import run_l1_pipeline_core


def test_l1_pipeline_all_green_on_clean_repo(
    sandbox, work_item_id, tenant_id, git_sha
):
    plan = PlanArtifact(
        work_item_id=work_item_id,
        expected_files=[],
        no_code_change=True,
        diff_budget_lines=400,
    )
    sha = git_sha()
    result = run_l1_pipeline_core(
        executor=sandbox,
        work_item_id=work_item_id,
        tenant_id=tenant_id,
        plan=plan,
        base_sha=sha,
        head_sha=sha,
    )
    assert result.passed is True
    checks = {f.check for f in result.findings}
    assert checks == {"lint", "typecheck", "test", "build", "sast", "secret_scan", "diff_budget", "forbidden_paths"}
    assert all(f.passed for f in result.findings)


def test_l1_pipeline_fails_clean_when_one_check_fails(
    sandbox, git_repo, feature_branch, work_item_id, tenant_id, git_sha
):
    base_sha = git_sha()
    feature_branch("test_broken.py", "def test_broken():\n    assert False\n")
    plan = PlanArtifact(work_item_id=work_item_id, expected_files=["test_broken.py"], diff_budget_lines=400)
    result = run_l1_pipeline_core(
        executor=sandbox,
        work_item_id=work_item_id,
        tenant_id=tenant_id,
        plan=plan,
        base_sha=base_sha,
        head_sha=git_sha(),
    )
    assert result.passed is False
    test_finding = next(f for f in result.findings if f.check == "test")
    assert test_finding.passed is False
    # os outros checks continuam rodando e reportando (não corta no meio, P6)
    assert len(result.findings) == 8


def test_l1_pipeline_persists_validation_run_and_audit_row(
    sandbox, work_item_id, tenant_id, git_sha
):
    plan = PlanArtifact(
        work_item_id=work_item_id,
        expected_files=[],
        no_code_change=True,
        diff_budget_lines=400,
    )
    sha = git_sha()
    result = run_l1_pipeline_core(
        executor=sandbox,
        work_item_id=work_item_id,
        tenant_id=tenant_id,
        plan=plan,
        base_sha=sha,
        head_sha=sha,
    )

    conn = psycopg2.connect("postgresql://dse_app:dse_app_dev_only@localhost:5432/dse")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT passed FROM validation_runs WHERE work_item_id = %s", (work_item_id,)
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == result.passed

        with conn.cursor() as cur:
            cur.execute(
                "SELECT action FROM audit_log WHERE work_item_id = %s AND action = 'l1_pipeline_run'",
                (work_item_id,),
            )
            audit_row = cur.fetchone()
        assert audit_row is not None, "P8: toda decisão consequente deve gerar linha em audit_log"
    finally:
        conn.close()


def test_l1_pipeline_missing_trusted_manifest_is_not_green(
    sandbox, git_repo, work_item_id, tenant_id, git_sha
):
    (git_repo / ".dse" / "validation.json").unlink()
    # O manifesto era confiável no commit anterior. Um novo base SHA sem ele
    # representa um repositório que ainda não configurou o L1.
    import subprocess

    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "remove validation manifest"],
        cwd=git_repo,
        check=True,
    )
    sha = git_sha()
    result = run_l1_pipeline_core(
        executor=sandbox,
        work_item_id=work_item_id,
        tenant_id=tenant_id,
        plan=PlanArtifact(work_item_id=work_item_id, no_code_change=True),
        base_sha=sha,
        head_sha=sha,
        persist=False,
    )

    assert result.passed is False
    assert result.status == GateStatus.NOT_CONFIGURED
    assert any(f.check == "l1_manifest" for f in result.findings)
