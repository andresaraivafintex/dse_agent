from __future__ import annotations

import os

import psycopg2
import pytest

DSN = os.environ.get(
    "DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
)
os.environ.setdefault("DSE_DATABASE_URL", DSN)


@pytest.fixture(autouse=True)
def _require_postgres():
    try:
        conn = psycopg2.connect(DSN, connect_timeout=3)
        conn.close()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Postgres indisponível em {DSN}: {exc}")
