from __future__ import annotations

import collections
import pathlib
import re

import pytest

from scripts import test_matrix, with_test_database

_MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent.parent / "migrations"

# Historical collision PREDATING this gate (0020_wsc4.sql + 0020_wse4.sql): both
# have already been applied (schema_migrations records by NAME; lexicographic
# ordering runs them stably) — renumbering would break every existing
# environment. Frozen here; NO new collision is accepted.
_GRANDFATHERED_PREFIXES = {"0020"}


def test_migration_numeric_prefixes_are_unique() -> None:
    prefixes = [
        re.match(r"(\d+)_", f.name).group(1)
        for f in sorted(_MIGRATIONS_DIR.glob("*.sql"))
        if re.match(r"(\d+)_", f.name)
    ]
    duplicated = {p for p, n in collections.Counter(prefixes).items() if n > 1}
    new_collisions = duplicated - _GRANDFATHERED_PREFIXES
    assert not new_collisions, (
        f"duplicated migration prefix: {sorted(new_collisions)} — reserve the next "
        "free number (CONVENTIONS.md, 'Migrations') instead of colliding"
    )


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
