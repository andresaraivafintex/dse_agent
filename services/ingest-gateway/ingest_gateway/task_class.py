"""Deterministic task_class classification (Plan 08 §A).

The task class feeds the ROI (human hours per class) and the Analytics
"by category" charts. Decided at INTAKE from the label (GitHub) / issue-type
(Jira) — a fixed map, no LLM decides (P1). Closed vocabulary:
bug_fix | feature_small | test_coverage | dependency_update | docs | refactor
| chore (default).
"""
from __future__ import annotations

TASK_CLASSES = (
    "bug_fix", "feature_small", "test_coverage",
    "dependency_update", "docs", "refactor", "chore",
)
DEFAULT_TASK_CLASS = "chore"

# GitHub label (lowercased) -> task_class. Covers the conventional labels + the
# dependabot ones. Precedence: the first non-chore class found wins, in the
# specificity order below.
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

# Jira issue-type (lowercased) -> task_class.
_JIRA_TYPE_MAP: dict[str, str] = {
    "bug": "bug_fix",
    "story": "feature_small",
    "new feature": "feature_small",
    "improvement": "feature_small",
    "task": "chore",
    "sub-task": "chore",
    "documentation": "docs",
}

# specificity order when there are multiple labels (the most informative wins).
_PRECEDENCE = (
    "bug_fix", "dependency_update", "test_coverage", "docs", "refactor",
    "feature_small", "chore",
)


def classify_task_class(labels: list[str] | None = None, issue_type: str | None = None) -> str:
    """Returns the task_class from the closed vocabulary. Deterministic: GitHub
    labels take priority (more granular); Jira issue-type as fallback; nothing
    matched -> chore."""
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
