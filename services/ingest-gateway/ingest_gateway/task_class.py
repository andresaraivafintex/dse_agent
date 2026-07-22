"""Classificação determinística de task_class (Plano 08 §A).

A classe da tarefa alimenta o ROI (horas humanas por classe) e os gráficos
"por categoria" do Analytics. Decidida no INTAKE por label (GitHub) /
issue-type (Jira) — mapa fixo, nenhum LLM decide (P1). Vocabulário fechado:
bug_fix | feature_small | test_coverage | dependency_update | docs | refactor
| chore (default).
"""
from __future__ import annotations

TASK_CLASSES = (
    "bug_fix", "feature_small", "test_coverage",
    "dependency_update", "docs", "refactor", "chore",
)
DEFAULT_TASK_CLASS = "chore"

# label do GitHub (lower) -> task_class. Cobre os labels convencionais + os do
# dependabot. Precedência: a primeira classe não-chore encontrada ganha, na
# ordem de especificidade abaixo.
_LABEL_MAP: dict[str, str] = {
    "bug": "bug_fix",
    "defect": "bug_fix",
    "regression": "bug_fix",
    "enhancement": "feature_small",
    "feature": "feature_small",
    "story": "feature_small",
    "test": "test_coverage",
    "tests": "test_coverage",
    "coverage": "test_coverage",
    "dependencies": "dependency_update",
    "dependency": "dependency_update",
    "deps": "dependency_update",
    "documentation": "docs",
    "docs": "docs",
    "refactor": "refactor",
    "refactoring": "refactor",
    "chore": "chore",
}

# issue-type do Jira (lower) -> task_class.
_JIRA_TYPE_MAP: dict[str, str] = {
    "bug": "bug_fix",
    "story": "feature_small",
    "new feature": "feature_small",
    "improvement": "feature_small",
    "task": "chore",
    "sub-task": "chore",
    "documentation": "docs",
}

# ordem de especificidade quando há múltiplos labels (o mais informativo ganha).
_PRECEDENCE = (
    "bug_fix", "dependency_update", "test_coverage", "docs", "refactor",
    "feature_small", "chore",
)


def classify_task_class(labels: list[str] | None = None, issue_type: str | None = None) -> str:
    """Retorna a task_class do vocabulário fechado. Determinístico: labels do
    GitHub têm prioridade (mais granulares); Jira issue-type como fallback;
    nada casou -> chore."""
    hits: set[str] = set()
    for label in labels or []:
        cls = _LABEL_MAP.get(str(label).strip().lower())
        if cls:
            hits.add(cls)
    if issue_type:
        cls = _JIRA_TYPE_MAP.get(str(issue_type).strip().lower())
        if cls:
            hits.add(cls)
    for cls in _PRECEDENCE:
        if cls in hits:
            return cls
    return DEFAULT_TASK_CLASS
