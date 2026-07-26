"""Proves `mint_virtual_key`/`revoke_virtual_key` keep the `virtual_keys` table
(migrations/0005_wsd.sql) as a faithful record on the DSE side — real Postgres,
no mock (P8: reconciliation has to be genuinely possible).
"""
from __future__ import annotations

from dse_contracts.gateway_contract import Stage
from model_gateway_client import db, mint_virtual_key, revoke_virtual_key


def _fetch_row(key_prefix: str):
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tenant_id, work_item_id, stage, status, revoked_at "
                "FROM virtual_keys WHERE key_prefix = %s",
                (key_prefix,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def test_mint_inserts_active_row(unique_ids):
    key = mint_virtual_key(
        unique_ids["tenant_id"], unique_ids["work_item_id"], Stage.coder, models=["eco/echo-model"]
    )
    try:
        row = _fetch_row(key[:12])
        assert row is not None
        tenant_id, work_item_id, stage, status, revoked_at = row
        assert tenant_id == unique_ids["tenant_id"]
        assert work_item_id == unique_ids["work_item_id"]
        assert stage == "coder"
        assert status == "active"
        assert revoked_at is None
    finally:
        revoke_virtual_key(key)


def test_revoke_marks_row_revoked(unique_ids):
    key = mint_virtual_key(
        unique_ids["tenant_id"], unique_ids["work_item_id"], Stage.coder, models=["eco/echo-model"]
    )
    revoke_virtual_key(key)

    row = _fetch_row(key[:12])
    assert row is not None
    _, _, _, status, revoked_at = row
    assert status == "revoked"
    assert revoked_at is not None


def test_key_hash_is_unique_per_mint(unique_ids):
    key_a = mint_virtual_key(unique_ids["tenant_id"], unique_ids["work_item_id"], Stage.coder)
    key_b = mint_virtual_key(unique_ids["tenant_id"], unique_ids["work_item_id"], Stage.coder)
    try:
        assert key_a != key_b
    finally:
        revoke_virtual_key(key_a)
        revoke_virtual_key(key_b)
