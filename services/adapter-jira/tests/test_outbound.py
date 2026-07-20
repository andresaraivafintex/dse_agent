"""WSA-E5-T3 — outbound: status comment único por ticket, editado in-place,
via a MESMA MutableCommentWriter (backend Jira novo). Jira via FakeJiraClient;
comment_state persistido em Postgres real."""
from __future__ import annotations

import json

import psycopg2

from dse_contracts.mutable_comment import MutableCommentWriter
from adapter_jira.backend import FakeJiraClient, JiraCommentBackend
from adapter_jira.comment_store import SURFACE, PgCommentStateStore

SUPERUSER_DSN = "postgresql://dse:dse_dev_only@localhost:5432/dse"
TENANT_ID = "test_tenant_jira_adapter"


def _make_work_item() -> str:
    wid = "wi_jira_outbound_test"
    conn = psycopg2.connect(SUPERUSER_DSN)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO work_items (id, tenant_id, source, source_ref, requester, idempotency_key) "
            "VALUES (%s,%s,'jira',%s::jsonb,'usr_a',%s) ON CONFLICT (id) DO NOTHING",
            (wid, TENANT_ID, json.dumps({"ticket_key": "DSE-500"}), f"idem_{wid}"),
        )
    conn.commit()
    conn.close()
    return wid


def test_single_status_comment_edited_in_place():
    work_item_id = _make_work_item()
    client = FakeJiraClient()
    writer = MutableCommentWriter(JiraCommentBackend(client), PgCommentStateStore(), SURFACE)

    ref1 = writer.upsert(work_item_id, {"ticket_key": "DSE-500"}, "Status: implementing")
    ref2 = writer.upsert(work_item_id, {"ticket_key": "DSE-500"}, "Status: pr_ready")

    # mesmo comment_ref -> foi editado, não recriado.
    assert ref1 == ref2
    comment_id = json.loads(ref1)["comment_id"]
    # exatamente 1 comentário existe no ticket, com o corpo final.
    assert client.comments["DSE-500"] == {comment_id: "Status: pr_ready"}

    conn = psycopg2.connect(SUPERUSER_DSN)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT comment_ref FROM comment_state WHERE work_item_id=%s AND surface=%s", (work_item_id, SURFACE)
        )
        assert json.loads(cur.fetchone()[0])["comment_id"] == comment_id
    conn.close()
