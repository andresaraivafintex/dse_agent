"""WSE-E1-T3 — compara o diff final contra o `PlanArtifact` (dse_contracts):
arquivos tocados vs `expected_files`, tamanho do diff vs `diff_budget_lines`,
e todo arquivo tocado vs `forbidden_paths`. Produz exatamente 2 `L1Finding`
(`diff_budget`, `forbidden_paths`) — cada um citando o plano na mensagem
quando falha (P8: evidência, não apenas um booleano opaco)."""
from __future__ import annotations

from dse_contracts import L1Finding, PlanArtifact

from dse_validation.sandbox_exec import SandboxExecutor


class DiffSummary:
    def __init__(self, files_changed: list[str], total_lines_changed: int):
        self.files_changed = files_changed
        self.total_lines_changed = total_lines_changed


def compute_diff_summary(executor: SandboxExecutor, base_branch: str, timeout: int = 60) -> DiffSummary:
    """`git diff --numstat <base_branch>...HEAD` dentro do sandbox — soma
    linhas adicionadas+removidas por arquivo (arquivos binários reportam
    "-" no numstat; contamos como arquivo tocado mas 0 linhas, para não
    quebrar em diffs com assets)."""
    result = executor.run(
        ["git", "diff", "--numstat", f"{base_branch}...HEAD"], timeout=timeout
    )
    if result.returncode != 0:
        raise RuntimeError(
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
    return DiffSummary(files_changed=files, total_lines_changed=total)


def _is_forbidden(path: str, forbidden_paths: list[str]) -> str | None:
    for forbidden in forbidden_paths:
        if path == forbidden or path.startswith(forbidden):
            return forbidden
    return None


def diff_budget_finding(diff: DiffSummary, plan: PlanArtifact) -> L1Finding:
    over_budget = diff.total_lines_changed > plan.diff_budget_lines
    expected = set(plan.expected_files)
    unexpected_files = [f for f in diff.files_changed if expected and f not in expected]
    # se o plano não declarou nenhum expected_files, não penalizamos (Fase 1:
    # PlanArtifact mínimo preenchido pelo próprio Coder, pode vir vazio).
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
    executor: SandboxExecutor, plan: PlanArtifact, base_branch: str
) -> list[L1Finding]:
    diff = compute_diff_summary(executor, base_branch)
    return [diff_budget_finding(diff, plan), forbidden_paths_finding(diff, plan)]
