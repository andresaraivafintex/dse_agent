"""Conformidade do patch contra plano e SHAs imutáveis.

O diff é sempre ``base_sha...head_sha``. Nomes de branch são mutáveis e
podem nem existir no clone do sandbox; aceitá-los aqui foi a causa da
regressão em que ``main...HEAD`` prendia o WorkItem.
"""
from __future__ import annotations

import re

from dse_contracts import GateStatus, L1Finding, PlanArtifact

from dse_validation.sandbox_exec import SandboxExecutor


class DiffSummary:
    def __init__(
        self,
        files_changed: list[str],
        total_lines_changed: int,
        *,
        base_sha: str,
        head_sha: str,
    ):
        self.files_changed = files_changed
        self.total_lines_changed = total_lines_changed
        self.base_sha = base_sha
        self.head_sha = head_sha


class DiffComputationError(RuntimeError):
    pass


_FULL_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")


def _verify_commit(executor: SandboxExecutor, sha: str, label: str, timeout: int) -> None:
    if not _FULL_GIT_SHA_RE.fullmatch(sha):
        raise DiffComputationError(
            f"{label} deve ser um SHA Git completo de 40 ou 64 caracteres hexadecimais"
        )
    result = executor.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], timeout=timeout)
    if not result.ok:
        raise DiffComputationError(f"{label}={sha} não existe como commit no sandbox")


def compute_diff_summary(
    executor: SandboxExecutor,
    base_sha: str,
    head_sha: str,
    timeout: int = 60,
) -> DiffSummary:
    """``git diff --numstat <base_sha>...<head_sha>`` dentro do sandbox — soma
    linhas adicionadas+removidas por arquivo (arquivos binários reportam
    "-" no numstat; contamos como arquivo tocado mas 0 linhas, para não
    quebrar em diffs com assets)."""
    _verify_commit(executor, base_sha, "base_sha", timeout)
    _verify_commit(executor, head_sha, "head_sha", timeout)
    result = executor.run(
        ["git", "diff", "--numstat", f"{base_sha}...{head_sha}"], timeout=timeout
    )
    if result.returncode != 0:
        raise DiffComputationError(
            f"git diff --numstat falhou (exit={result.returncode}): {result.stderr.strip()}"
        )
    files: list[str] = []
    total = 0
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, path = parts
        files.append(path)
        if added.isdigit():
            total += int(added)
        if removed.isdigit():
            total += int(removed)
    return DiffSummary(
        files_changed=files,
        total_lines_changed=total,
        base_sha=base_sha,
        head_sha=head_sha,
    )


def _is_forbidden(path: str, forbidden_paths: list[str]) -> str | None:
    for forbidden in forbidden_paths:
        if path == forbidden or path.startswith(forbidden):
            return forbidden
    return None


def diff_budget_finding(diff: DiffSummary, plan: PlanArtifact) -> L1Finding:
    if not plan.expected_files and not plan.no_code_change:
        return L1Finding(
            check="diff_budget",
            passed=False,
            status=GateStatus.NOT_CONFIGURED,
            detail=(
                "PlanArtifact.expected_files está vazio para uma tarefa de código; "
                "use no_code_change=true somente quando nenhum patch for esperado"
            ),
        )

    if plan.no_code_change and diff.files_changed:
        return L1Finding(
            check="diff_budget",
            passed=False,
            status=GateStatus.FAIL,
            detail=(
                "PlanArtifact.no_code_change=true, mas o diff imutável "
                f"{diff.base_sha[:12]}...{diff.head_sha[:12]} alterou {diff.files_changed}"
            ),
        )

    over_budget = diff.total_lines_changed > plan.diff_budget_lines
    expected = set(plan.expected_files)
    # Arquivos de TESTE não contam como fora-do-plano (achado do disparo real):
    # o Tester escreve testes por design DEPOIS do plano — o Planner nunca os
    # lista. Os forbidden_paths continuam valendo para eles (check separado);
    # aqui só deixamos de reprovar o estágio legítimo do próprio sistema.
    from dse_contracts.paths import is_test_path

    unexpected_files = [
        f for f in diff.files_changed if f not in expected and not is_test_path(f)
    ]
    passed = not over_budget and not unexpected_files
    if passed:
        detail = (
            f"diff dentro do orçamento: {diff.total_lines_changed}/{plan.diff_budget_lines} linhas, "
            f"{len(diff.files_changed)} arquivo(s), todos declarados no plano"
        )
        return L1Finding(check="diff_budget", passed=True, detail=detail)

    reasons = []
    if over_budget:
        reasons.append(
            f"diff de {diff.total_lines_changed} linhas excede diff_budget_lines={plan.diff_budget_lines} do PlanArtifact"
        )
    if unexpected_files:
        reasons.append(
            "arquivo(s) tocado(s) fora de PlanArtifact.expected_files="
            f"{sorted(expected)}: {unexpected_files}"
        )
    return L1Finding(check="diff_budget", passed=False, detail="; ".join(reasons))


def forbidden_paths_finding(diff: DiffSummary, plan: PlanArtifact) -> L1Finding:
    violations: list[tuple[str, str]] = []
    for f in diff.files_changed:
        hit = _is_forbidden(f, plan.forbidden_paths)
        if hit:
            violations.append((f, hit))

    if not violations:
        return L1Finding(
            check="forbidden_paths",
            passed=True,
            detail=f"nenhum arquivo tocado sob forbidden_paths do plano ({plan.forbidden_paths})",
        )

    detail = "; ".join(
        f"{f} está sob path proibido pelo PlanArtifact.forbidden_paths='{hit}'" for f, hit in violations
    )
    return L1Finding(check="forbidden_paths", passed=False, detail=detail)


def plan_compliance_findings(
    executor: SandboxExecutor,
    plan: PlanArtifact,
    base_sha: str,
    head_sha: str,
) -> list[L1Finding]:
    try:
        diff = compute_diff_summary(executor, base_sha, head_sha)
    except DiffComputationError as exc:
        return [
            L1Finding(
                check="git_diff",
                passed=False,
                status=GateStatus.ERROR,
                detail=str(exc),
            )
        ]
    return [diff_budget_finding(diff, plan), forbidden_paths_finding(diff, plan)]
