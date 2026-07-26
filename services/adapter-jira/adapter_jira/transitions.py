"""WSA-E5-T3 — status transitions SERIALIZED per ticket.

Jira Cloud rejects concurrent transitions on the same issue. Transitions are
enqueued in `jira_transition_queue` (migrations/0008_wsa2.sql) and drained by a
worker that guarantees, via a per-ticket advisory lock, that only ONE transition
per ticket runs at a time — different tickets proceed in parallel.

`enqueue_transition` is idempotent by `dedup_key` (ON CONFLICT DO NOTHING): the
same transition request (same origin/state) never enqueues twice.
"""
from __future__ import annotations

import logging

from dse_audit import emit as audit_emit

from .backend import JiraClientLike
from ingest_gateway.db import get_connection

logger = logging.getLogger("adapter_jira.transitions")

_MAX_ATTEMPTS = 5


def enqueue_transition(
    conn,
    *,
    tenant_id: str,
    ticket_key: str,
    target_status: str,
    dedup_key: str,
    work_item_id: str | None = None,
) -> bool:
    """Enqueues a transition. Returns True if it enqueued (new), False if it
    already existed (dedup by `dedup_key`). Does not commit — the caller
    controls the transaction."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO jira_transition_queue
                (tenant_id, ticket_key, target_status, work_item_id, dedup_key)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (dedup_key) DO NOTHING
            RETURNING id
            """,
            (tenant_id, ticket_key, target_status, work_item_id, dedup_key),
        )
        return cur.fetchone() is not None


class TransitionWorker:
    """Drains `jira_transition_queue`, serializing per ticket. Usage:
    `TransitionWorker(client).drain_once()` in a loop (process separate from
    FastAPI, same idea as ingest-gateway's `dispatcher_main`). Synchronous: the
    Jira transport is `requests` (blocking), and this worker does not run on the
    app's event loop."""

    def __init__(self, client: JiraClientLike, *, batch_tickets: int = 50, conn_factory=get_connection):
        self._client = client
        self._batch_tickets = batch_tickets
        self._conn_factory = conn_factory

    def drain_once(self) -> int:
        conn = self._conn_factory()
        conn.autocommit = True  # advisory locks are session-scoped; we control manual commits per ticket
        processed = 0
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT ticket_key FROM jira_transition_queue WHERE NOT processed AND attempts < %s LIMIT %s",
                    (_MAX_ATTEMPTS, self._batch_tickets),
                )
                tickets = [r[0] for r in cur.fetchall()]

            for ticket_key in tickets:
                processed += self._drain_ticket(conn, ticket_key)
            return processed
        finally:
            conn.close()

    def _drain_ticket(self, conn, ticket_key: str) -> int:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (ticket_key,))
            got_lock = cur.fetchone()[0]
        if not got_lock:
            # Another worker is transitioning this ticket — skip (serialization).
            return 0

        processed = 0
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, target_status, work_item_id, tenant_id
                    FROM jira_transition_queue
                    WHERE ticket_key = %s AND NOT processed AND attempts < %s
                    ORDER BY id
                    """,
                    (ticket_key, _MAX_ATTEMPTS),
                )
                rows = cur.fetchall()

            for row_id, target_status, work_item_id, tenant_id in rows:
                ok = self._apply_transition(conn, ticket_key, row_id, target_status, work_item_id, tenant_id)
                if not ok:
                    # Order matters — do not skip transitions ahead in this ticket.
                    break
                processed += 1
            return processed
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (ticket_key,))

    def _apply_transition(self, conn, ticket_key, row_id, target_status, work_item_id, tenant_id) -> bool:
        try:
            transitions = self._client.get_transitions(ticket_key)
            match = next((t for t in transitions if t.get("to_status") == target_status or t.get("name") == target_status), None)
            if match is None:
                raise ValueError(f"no transition available for status '{target_status}' on {ticket_key}")
            self._client.transition_issue(ticket_key, match["id"])
        except Exception as exc:  # noqa: BLE001 — caught in order to record and retry
            logger.exception("transition failed ticket=%s target=%s", ticket_key, target_status)
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE jira_transition_queue SET attempts = attempts + 1, last_error = %s WHERE id = %s",
                    (str(exc)[:500], row_id),
                )
            audit_emit(
                actor="system:adapter-jira",
                action="jira_transition_failed",
                tenant_id=tenant_id,
                work_item_id=work_item_id,
                details={"ticket_key": ticket_key, "target_status": target_status, "error": str(exc)[:200]},
            )
            return False

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE jira_transition_queue SET processed = true, processed_at = now() WHERE id = %s",
                (row_id,),
            )
        audit_emit(
            actor="system:adapter-jira",
            action="jira_transition_applied",
            tenant_id=tenant_id,
            work_item_id=work_item_id,
            details={"ticket_key": ticket_key, "target_status": target_status},
        )
        return True
