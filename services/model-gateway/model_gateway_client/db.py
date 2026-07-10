"""Conexão Postgres para a tabela `virtual_keys` (migrations/0005_wsd.sql).
Não usa nenhum ORM — psycopg2 puro, mesma convenção de `dse_audit.client`."""
from __future__ import annotations

import psycopg2

from . import settings


def get_connection():
    return psycopg2.connect(settings.virtual_keys_database_url())
