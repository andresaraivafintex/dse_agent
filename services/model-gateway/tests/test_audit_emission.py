"""P8: toda emissão/revogação de virtual key grava uma linha no audit ledger
via `dse_audit.emit` — nunca por fora dele. Consulta o Postgres real
(audit_log é append-only, sem UPDATE/DELETE grant — ver migrations/0001)."""
from __future__ import annotations

from dse_audit.client import get_connection
from dse_contracts.gateway_contract import Stage
from model_gateway_client import mint_virtual_key, revoke_virtual_key


def _fetch_actions(work_item_id: str) -> list[str]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT action FROM audit_log WHERE work_item_id = %s AND actor = %s ORDER BY id",
                (work_item_id, "system:model-gateway"),
            )
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def test_mint_and_revoke_emit_audit_rows(unique_ids):
    tenant_id = unique_ids["tenant_id"]
    work_item_id = unique_ids["work_item_id"]

    key = mint_virtual_key(tenant_id, work_item_id, Stage.coder, models=["eco/echo-model"])
    actions_after_mint = _fetch_actions(work_item_id)
    assert "virtual_key.issued" in actions_after_mint

    revoke_virtual_key(key)
    actions_after_revoke = _fetch_actions(work_item_id)
    assert actions_after_revoke == ["virtual_key.issued", "virtual_key.revoked"]


def test_audit_row_details_carry_tenant_and_stage(unique_ids):
    tenant_id = unique_ids["tenant_id"]
    work_item_id = unique_ids["work_item_id"]
    key = mint_virtual_key(tenant_id, work_item_id, Stage.coder, models=["eco/echo-model"])
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT tenant_id, details FROM audit_log "
                    "WHERE work_item_id = %s AND action = 'virtual_key.issued'",
                    (work_item_id,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None
        row_tenant_id, details = row
        assert row_tenant_id == tenant_id
        assert details["stage"] == "coder"
        assert details["models"] == ["eco/echo-model"]
    finally:
        revoke_virtual_key(key)


def test_revoke_of_untracked_key_does_not_emit_audit_row():
    """revoke_virtual_key falha limpo (VirtualKeyNotFoundError) ANTES de
    qualquer decisão consequente — não deve gravar nada no ledger para uma
    key que não existe no nosso rastreamento."""
    import pytest
    from model_gateway_client.errors import VirtualKeyNotFoundError

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM audit_log WHERE actor = 'system:model-gateway'")
            before = cur.fetchone()[0]
    finally:
        conn.close()

    with pytest.raises(VirtualKeyNotFoundError):
        revoke_virtual_key("sk-definitely-not-tracked-xyz")

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM audit_log WHERE actor = 'system:model-gateway'")
            after = cur.fetchone()[0]
    finally:
        conn.close()
    assert after == before
