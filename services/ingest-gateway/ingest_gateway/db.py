"""Conexão Postgres compartilhada pelo pacote ingest_gateway.

Mesma convenção de `dse_audit`/`dse_identity`: DSN via env var, role `dse_app`
(sem grants de UPDATE/DELETE em audit_log; grants normais nas tabelas do
WS-A criadas em migrations/0002_wsa.sql).
"""
from __future__ import annotations

import os

import psycopg2

_DSN = os.environ.get(
    "DSE_INGEST_DATABASE_URL",
    os.environ.get("DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"),
)


def get_connection():
    """Nova conexão psycopg2. Caller controla commit/rollback/close (usada
    tanto para escrita transacional isolada quanto participando de uma
    transação maior, seguindo a mesma convenção de `dse_audit.emit(conn=...)`).
    """
    return psycopg2.connect(_DSN)
