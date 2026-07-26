"""task_class classifier (Plano 08 §A) — deterministic, closed vocabulary."""
from ingest_gateway.task_class import classify_task_class, TASK_CLASSES, DEFAULT_TASK_CLASS


def test_github_labels():
    assert classify_task_class(labels=["bug"]) == "bug_fix"
    assert classify_task_class(labels=["enhancement"]) == "feature_small"
    assert classify_task_class(labels=["dependencies"]) == "dependency_update"
    assert classify_task_class(labels=["documentation"]) == "docs"
    assert classify_task_class(labels=["test"]) == "test_coverage"


def test_jira_issue_type():
    assert classify_task_class(issue_type="Bug") == "bug_fix"
    assert classify_task_class(issue_type="Story") == "feature_small"
    assert classify_task_class(issue_type="Task") == "chore"


def test_precedence_most_specific_wins():
    # bug beats documentation/feature when both are present
    assert classify_task_class(labels=["documentation", "bug", "enhancement"]) == "bug_fix"


def test_default_and_closed_vocabulary():
    assert classify_task_class(labels=["random-label"]) == DEFAULT_TASK_CLASS
    assert classify_task_class() == DEFAULT_TASK_CLASS
    for c in TASK_CLASSES:
        assert isinstance(c, str)
    # everything the classifier emits is in the closed vocabulary
    for labels in (["bug"], ["enhancement"], ["deps"], ["docs"], ["refactor"], ["test"], []):
        assert classify_task_class(labels=labels) in TASK_CLASSES
