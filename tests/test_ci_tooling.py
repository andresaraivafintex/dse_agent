from __future__ import annotations

import pytest

from scripts import test_matrix, with_test_database


def test_test_matrix_registers_every_suite_exactly_once() -> None:
    test_matrix._validate_manifest()
    registered = test_matrix._registered_suites(test_matrix.SUITE_GROUPS)
    assert len(registered) == len(set(registered))
    assert set(registered) == test_matrix._discovered_suites()
    assert set(registered) == set(test_matrix.SUITE_COVERAGE_TARGETS)


def test_suite_selection_takes_precedence_over_group(monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["test_matrix.py", "--group", "all", "--suite", "packages/contracts", "--list"],
    )
    args = test_matrix.parse_args()
    assert args.suite == ["packages/contracts"]


def test_database_url_replaces_only_database_path() -> None:
    result = with_test_database._database_url(
        "postgresql://user:password@localhost:5432/original?sslmode=disable",
        "dse_test_abcdefgh",
    )
    assert result == (
        "postgresql://user:password@localhost:5432/dse_test_abcdefgh?sslmode=disable"
    )


@pytest.mark.parametrize("target", ["dse", "public", "dse_test_bad-name", ""])
def test_cleanup_guard_rejects_unsafe_target(target: str) -> None:
    with pytest.raises(SystemExit, match="unsafe"):
        with_test_database._assert_safe_target(
            "postgresql://dse:dev@localhost:5432/dse", target
        )


def test_cleanup_guard_rejects_remote_database_by_default(monkeypatch) -> None:
    monkeypatch.delenv("DSE_ALLOW_REMOTE_TEST_DB", raising=False)
    with pytest.raises(SystemExit, match="non-local"):
        with_test_database._assert_safe_target(
            "postgresql://dse:dev@db.example.test:5432/dse",
            "dse_test_abcdefgh",
        )
