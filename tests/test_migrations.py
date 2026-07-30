"""Guards on the migration corpus, checked against how the runner executes it."""

from __future__ import annotations

import pathlib
import re

_MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent.parent / "migrations"

# scripts/migrate.py connects with autocommit off and commits once per file, so
# every statement in a .sql file runs inside a transaction block. PostgreSQL
# refuses these there, and the blast radius is not the file: the migration Job is
# a Helm post-upgrade hook, so the abort fails the release with every file
# ordered before it already applied and committed. The online equivalents have to
# be run by hand outside the runner — 0035 documents one.
_OUTSIDE_TRANSACTION_ONLY = re.compile(
    r"\b(?:(?:CREATE|DROP)\s+INDEX\s+CONCURRENTLY"
    r"|REINDEX\b[^;]*?\bCONCURRENTLY"
    r"|VACUUM\b"
    r"|CREATE\s+DATABASE\b"
    r"|ALTER\s+SYSTEM\b)",
    re.IGNORECASE,
)


def _executable_sql(text: str) -> str:
    """The file minus its comments: a migration is free to DESCRIBE a statement
    it must not run, which is exactly what 0035 does with the manual build."""
    return re.sub(r"--[^\n]*", " ", re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL))


def test_no_migration_statement_requires_running_outside_a_transaction() -> None:
    offenders: dict[str, list[str]] = {}
    for migration in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        found = _OUTSIDE_TRANSACTION_ONLY.findall(_executable_sql(migration.read_text()))
        if found:
            offenders[migration.name] = found
    assert not offenders, (
        f"statement PostgreSQL refuses inside a transaction block: {offenders} — "
        "scripts/migrate.py wraps every file in one, so this aborts the migration "
        "Job and fails the helm upgrade; run it by hand outside the runner instead"
    )
