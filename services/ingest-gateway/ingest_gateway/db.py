"""Postgres connection shared by the ingest_gateway package.

Same convention as `dse_audit`/`dse_identity`: DSN via env var, role `dse_app`
(no UPDATE/DELETE grants on audit_log; normal grants on the WS-A tables
created in migrations/0002_wsa.sql).
"""
from __future__ import annotations

import os

import psycopg2

_DSN = os.environ.get(
    "DSE_INGEST_DATABASE_URL",
    os.environ.get("DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"),
)


def get_connection():
    """New psycopg2 connection. The caller controls commit/rollback/close (used
    both for an isolated transactional write and as part of a larger
    transaction, following the same convention as `dse_audit.emit(conn=...)`).
    """
    return psycopg2.connect(_DSN)
