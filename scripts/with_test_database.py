#!/usr/bin/env python3
"""Run a command against disposable PostgreSQL state.

The default strategy creates a dedicated schema and exports ``PGOPTIONS`` so
even legacy test DSNs that explicitly name the development database resolve to
the isolated schema.  A full disposable database is also available.  Names are
generated here and guarded by a strict prefix; configured or pre-existing
objects are never selected as deletion targets.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import SplitResult, urlsplit, urlunsplit

import psycopg2
from psycopg2 import sql


ROOT = Path(__file__).resolve().parent.parent
LOCAL_HOSTS = {None, "", "localhost", "127.0.0.1", "::1"}
SAFE_DATABASE = re.compile(r"^dse_test_[a-z0-9_]{8,54}$")


def _database_url(base_url: str, database: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise SystemExit("test database URL must use postgres:// or postgresql://")
    return urlunsplit(
        SplitResult(parsed.scheme, parsed.netloc, f"/{database}", parsed.query, parsed.fragment)
    )


def _assert_safe_target(base_url: str, database: str) -> None:
    parsed = urlsplit(base_url)
    if parsed.hostname not in LOCAL_HOSTS and os.environ.get("DSE_ALLOW_REMOTE_TEST_DB") != "1":
        raise SystemExit(
            "refusing to create a disposable database on a non-local host; "
            "set DSE_ALLOW_REMOTE_TEST_DB=1 only for a dedicated CI database server"
        )
    if not SAFE_DATABASE.fullmatch(database):
        raise SystemExit(f"refusing unsafe disposable database name: {database!r}")


def _admin_connection(base_url: str):
    parsed = urlsplit(base_url)
    maintenance_db = parsed.path.strip("/") or "postgres"
    return psycopg2.connect(_database_url(base_url, maintenance_db))


def _create_database(base_url: str, database: str) -> None:
    connection = _admin_connection(base_url)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    finally:
        connection.close()


def _create_schema(base_url: str, schema: str) -> None:
    connection = psycopg2.connect(base_url)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE SCHEMA {} AUTHORIZATION CURRENT_USER").format(sql.Identifier(schema)))
    finally:
        connection.close()


def _grant_schema_usage(base_url: str, schema: str) -> None:
    connection = psycopg2.connect(base_url)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO dse_app").format(sql.Identifier(schema))
            )
            # Historical migrations grant sequences in ``public`` explicitly.
            # In a disposable-schema run those sequences live in the generated
            # schema, so mirror the production grant without widening table
            # privileges (audit UPDATE/DELETE remains revoked).
            cursor.execute(
                sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {} TO dse_app").format(
                    sql.Identifier(schema)
                )
            )
    finally:
        connection.close()


def _drop_schema(base_url: str, schema: str) -> None:
    _assert_safe_target(base_url, schema)
    connection = psycopg2.connect(base_url)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
    finally:
        connection.close()


def _drop_database(base_url: str, database: str) -> None:
    _assert_safe_target(base_url, database)
    connection = _admin_connection(base_url)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database,),
            )
            cursor.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database)))
    finally:
        connection.close()


def _migrate(admin_database_url: str, environment: dict[str, str]) -> None:
    migration_env = environment.copy()
    migration_env["DSE_DATABASE_URL"] = admin_database_url
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "migrate.py")],
        cwd=ROOT,
        env=migration_env,
        check=True,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="keep the database for diagnosis")
    parser.add_argument(
        "--isolation",
        choices=("schema", "database"),
        default=os.environ.get("DSE_TEST_DB_ISOLATION", "schema"),
        help="disposable state boundary (default: schema)",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("a command is required after --")
    return args


def main() -> int:
    args = parse_args()
    run_id = re.sub(r"[^a-z0-9_]", "_", os.environ.get("DSE_TEST_RUN_ID", "local").lower())
    target = f"dse_test_{run_id[:24]}_{uuid.uuid4().hex[:12]}"
    admin_base_url = os.environ.get(
        "DSE_TEST_ADMIN_DATABASE_URL",
        "postgresql://dse:dse_dev_only@localhost:5432/dse",
    )
    app_base_url = os.environ.get(
        "DSE_TEST_APP_DATABASE_URL",
        "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse",
    )
    _assert_safe_target(admin_base_url, target)

    environment = os.environ.copy()
    if args.isolation == "database":
        admin_test_url = _database_url(admin_base_url, target)
        app_test_url = _database_url(app_base_url, target)
    else:
        admin_test_url = admin_base_url
        app_test_url = app_base_url
        environment["PGOPTIONS"] = f"-c search_path={target},public"
    environment.update(
        {
            "DSE_ENVIRONMENT": "test",
            "DSE_TEST_RUN_ID": run_id,
            "DSE_DATABASE_URL": app_test_url,
            "DSE_AUDIT_DATABASE_URL": app_test_url,
            "DSE_IDENTITY_DATABASE_URL": app_test_url,
            "DSE_TEST_DATABASE_URL": app_test_url,
            "DSE_TEST_SUPERUSER_DATABASE_URL": admin_test_url,
        }
    )

    print(f"Creating disposable {args.isolation} {target}", flush=True)
    if args.isolation == "database":
        _create_database(admin_base_url, target)
    else:
        _create_schema(admin_base_url, target)
    exit_code = 1
    try:
        _migrate(admin_test_url, environment)
        if args.isolation == "schema":
            _grant_schema_usage(admin_base_url, target)
        exit_code = subprocess.run(args.command, cwd=ROOT, env=environment, check=False).returncode
    finally:
        if args.keep:
            print(f"Keeping disposable {args.isolation} for diagnosis: {target}", flush=True)
        else:
            print(f"Dropping disposable {args.isolation} {target}", flush=True)
            if args.isolation == "database":
                _drop_database(admin_base_url, target)
            else:
                _drop_schema(admin_base_url, target)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
