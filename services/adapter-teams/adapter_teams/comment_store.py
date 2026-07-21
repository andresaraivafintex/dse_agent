"""`CommentStateStore` (dse_contracts.mutable_comment) persistido em Postgres
(`comment_state`, migrations/0002_wsa.sql) — mantém o adapter 100% stateless,
igual aos adapters Slack/GitHub/Jira. `surface = "teams"`."""
from __future__ import annotations

from ingest_gateway.db import get_connection

SURFACE = "teams"


class PgCommentStateStore:
    def get_ref(self, work_item_id: str, surface: str) -> str | None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT comment_ref FROM comment_state WHERE work_item_id = %s AND surface = %s",
                    (work_item_id, surface),
                )
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            conn.close()

    def save_ref(self, work_item_id: str, surface: str, comment_ref: str) -> None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO comment_state (work_item_id, surface, comment_ref)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (work_item_id, surface)
                    DO UPDATE SET comment_ref = EXCLUDED.comment_ref
                    """,
                    (work_item_id, surface, comment_ref),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
