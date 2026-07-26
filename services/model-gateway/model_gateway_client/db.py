"""Postgres connection for the `virtual_keys` table (migrations/0005_wsd.sql).
No ORM here — plain psycopg2, same convention as `dse_audit.client`."""
from __future__ import annotations

import psycopg2

from . import settings


def get_connection():
    return psycopg2.connect(settings.virtual_keys_database_url())
