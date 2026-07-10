#!/usr/bin/env python3
"""Aplica migrações SQL puras em migrations/*.sql em ordem lexicográfica.

Idempotente: cada arquivo aplicado é registrado em schema_migrations e nunca
reaplicado. Sem framework de migration (P7 boring-first) — os próprios .sql
são idempotentes internamente (IF NOT EXISTS / ON CONFLICT) para permitir
reexecução manual segura durante desenvolvimento.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg2

DSN = os.environ.get(
    "DSE_DATABASE_URL", "postgresql://dse:dse_dev_only@localhost:5432/dse"
)
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def main() -> int:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print("Nenhuma migração encontrada em", MIGRATIONS_DIR)
        return 0

    conn = psycopg2.connect(DSN)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
        conn.commit()

        for f in files:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM schema_migrations WHERE filename = %s", (f.name,)
                )
                already = cur.fetchone() is not None
            if already:
                print(f"skip  {f.name} (já aplicada)")
                continue
            print(f"apply {f.name}")
            sql = f.read_text()
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s) "
                    "ON CONFLICT DO NOTHING",
                    (f.name,),
                )
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print("Migrações em dia.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
