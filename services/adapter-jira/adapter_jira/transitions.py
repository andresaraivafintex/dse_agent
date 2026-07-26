"""WSA-E5-T3 — status transitions SERIALIZED per ticket.

Jira Cloud rejects concurrent transitions on the same issue. Transitions are
enqueued in `jira_transition_queue` (migrations/0008_wsa2.sql) and drained by a
worker that guarantees, via a per-ticket advisory lock, that only ONE transition
per ticket runs at a time — different tickets proceed in parallel.

`enqueue_transition` is idempotent by `dedup_key` (ON CONFLICT DO NOTHING): the
same transition request (same origin/state) never enqueues twice.

THREE KINDS OF FAILURE, and they are not handled the same way
-------------------------------------------------------------
A transition can fail because Jira was momentarily unavailable, because the
target column DOES NOT EXIST on the board, or because it exists but is not
reachable from where the card is standing right now.

The middle one is a CONFIGURATION fact: retrying it changes nothing, and treating
it as transient is what happened when the real board had no "Blocked" column —
five transitions retried to exhaustion and, worse, each one blocked every later
transition queued for its ticket (ordering is preserved, so the queue stops at
the first row that will not settle). So it is settled once, recorded with what
the board DOES offer (an operator can then create exactly what is missing), and
the queue moves on.

The third one must NOT be settled, and telling the two apart needs a second
question. `GET /issue/{key}/transitions` returns only the edges available FROM
the issue's current status, never the board's status set — so "no transition to
Done" from a card sitting in Backlog says nothing about whether Done exists.
Settling on that discards a mirror update that would have succeeded a minute
later, and files an audit row whose reason ("not on this board") is simply
false. `client.project_statuses` answers the configuration question directly;
when it cannot answer, the row is treated as transient, because settling on an
unanswered lookup is the one outcome that loses data.

Neither transient case retries forever: attempts are counted, and the row is
settled with `jira_transition_abandoned` when they run out — the queue used to
just stop selecting it and leave no trace of why. The work item's status in
Postgres is the system of record; the Jira column is a mirror, and failing to
mirror must never stall the task nor happen silently.
"""
from __future__ import annotations

import logging
from typing import Literal

from dse_audit import emit as audit_emit

from .backend import JiraClientLike
from ingest_gateway.db import get_connection

logger = logging.getLogger("adapter_jira.transitions")

_MAX_ATTEMPTS = 5

# How many transition names go into the audit row. Bounded because the audit log
# is append-only: a board with a pathological workflow must not be able to write
# an unbounded blob there.
_MAX_REPORTED_TRANSITIONS = 40

# Outcome of one queue row. `unavailable` is settled-and-done exactly like
# `applied` — the difference is only WHY it will never run — while `failed` is
# the transient case that keeps its row for the next drain.
Outcome = Literal["applied", "unavailable", "failed"]


def _project_of(ticket_key: str) -> str:
    """`DSE-123` -> `DSE`. Jira keys are `<PROJECT>-<number>` by construction, so
    this needs no lookup (same derivation as `events.project_key`)."""
    return ticket_key.split("-", 1)[0]


def _to_statuses(transitions) -> list[str]:
    """Target statuses reachable from where the issue is standing right now."""
    return sorted({(t.get("to_status") or "") for t in transitions} - {""})


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
        """Returns how many queue rows this pass SETTLED — applied, or recognised
        as impossible on this board — i.e. how many will not be seen again.

        A row abandoned after `_MAX_ATTEMPTS` is settled too but deliberately not
        counted here: the caller uses this only to decide whether to sleep, and
        giving up on a transition is not progress worth spinning for. Its
        `jira_transition_abandoned` row is the record.
        """
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
                outcome = self._apply_transition(conn, ticket_key, row_id, target_status, work_item_id, tenant_id)
                if outcome == "failed":
                    # Order matters — do not skip transitions ahead in this ticket.
                    break
                # `unavailable` keeps going: the row is settled, and a column the
                # board never had must not hold back the transitions behind it.
                processed += 1
            return processed
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (ticket_key,))

    def _apply_transition(self, conn, ticket_key, row_id, target_status, work_item_id, tenant_id) -> Outcome:
        try:
            transitions = self._client.get_transitions(ticket_key)
        except Exception as exc:  # noqa: BLE001 — reading the workflow failed, which says nothing about the board
            self._record_transient_failure(
                conn, row_id, ticket_key, target_status, work_item_id, tenant_id, error=str(exc)
            )
            return "failed"

        match = next(
            (t for t in transitions if t.get("to_status") == target_status or t.get("name") == target_status),
            None,
        )
        if match is None:
            return self._classify_no_transition(
                conn, row_id, ticket_key, target_status, work_item_id, tenant_id, transitions
            )

        try:
            self._client.transition_issue(ticket_key, match["id"])
        except Exception as exc:  # noqa: BLE001 — caught in order to record and retry
            self._record_transient_failure(
                conn, row_id, ticket_key, target_status, work_item_id, tenant_id, error=str(exc)
            )
            return "failed"

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
        return "applied"

    def _board_statuses(self, ticket_key: str) -> set[str] | None:
        """The project's workflow statuses, or None when they cannot be had.

        Tolerant on purpose: a client that has no `project_statuses` at all, or one
        that raises, must not take down the drain loop — this lookup exists only to
        classify a failure, and "unknown" is a safe answer (the row is kept).
        """
        getter = getattr(self._client, "project_statuses", None)
        if not callable(getter):
            return None
        try:
            return getter(_project_of(ticket_key))
        except Exception:  # noqa: BLE001 — classifying must never be fatal
            logger.warning("could not read the project workflow for %s", ticket_key, exc_info=True)
            return None

    def _classify_no_transition(
        self, conn, row_id, ticket_key, target_status, work_item_id, tenant_id, transitions
    ) -> Outcome:
        """Decides WHY there is no edge into `target_status`, then acts on it.

        `transitions` only describes the edges out of the issue's CURRENT status,
        so its emptiness cannot distinguish "this column does not exist" from
        "this card is not somewhere that column can be reached from". Asking the
        project for its workflow statuses answers the first question on its own,
        and only that answer may settle the row: a card that has not moved yet
        will move, and discarding its mirror update is unrecoverable.
        """
        board = self._board_statuses(ticket_key)
        if board is not None and target_status not in board:
            self._settle_unavailable(
                conn, row_id, ticket_key, target_status, work_item_id, tenant_id, transitions, board
            )
            return "unavailable"

        # Exists on the board (or the board did not answer): transient. Counted
        # like any other attempt, so it cannot retry forever either.
        available_statuses = _to_statuses(transitions)
        self._record_transient_failure(
            conn,
            row_id,
            ticket_key,
            target_status,
            work_item_id,
            tenant_id,
            error=(
                f"target status '{target_status}' is not reachable from the issue's current status"
                if board is not None
                else f"no transition to '{target_status}' and the project's workflow statuses could not be read"
            ),
            action="jira_transition_not_reachable",
            extra={
                "reason": (
                    "target_status_not_reachable_from_current"
                    if board is not None
                    else "board_statuses_unknown"
                ),
                "available_statuses": available_statuses[:_MAX_REPORTED_TRANSITIONS],
            },
        )
        return "failed"

    def _record_transient_failure(
        self,
        conn,
        row_id,
        ticket_key,
        target_status,
        work_item_id,
        tenant_id,
        *,
        error: str,
        action: str = "jira_transition_failed",
        extra: dict | None = None,
    ) -> None:
        """Counts one attempt and keeps the row for the next drain — until the
        attempts run out, at which point the row is SETTLED and says so.

        Before, an exhausted row simply stopped matching `attempts < _MAX_ATTEMPTS`
        and sat in the queue forever with nothing recorded about being given up
        on. An operator reading the table could not tell it apart from a row still
        waiting its turn.
        """
        logger.warning(
            "transition not applied ticket=%s target=%s: %s", ticket_key, target_status, error
        )
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE jira_transition_queue SET attempts = attempts + 1, last_error = %s "
                "WHERE id = %s RETURNING attempts",
                (error[:500], row_id),
            )
            row = cur.fetchone()
        attempts = row[0] if row else None
        details = {
            "ticket_key": ticket_key,
            "target_status": target_status,
            "error": error[:200],
            "attempts": attempts,
            **(extra or {}),
        }

        if attempts is not None and attempts >= _MAX_ATTEMPTS:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE jira_transition_queue SET processed = true, processed_at = now(), last_error = %s "
                    "WHERE id = %s",
                    (f"abandoned_after_{attempts}_attempts: {error}"[:500], row_id),
                )
            audit_emit(
                actor="system:adapter-jira",
                action="jira_transition_abandoned",
                tenant_id=tenant_id,
                work_item_id=work_item_id,
                details={**details, "last_action": action},
            )
            return

        audit_emit(
            actor="system:adapter-jira",
            action=action,
            tenant_id=tenant_id,
            work_item_id=work_item_id,
            details=details,
        )

    def _settle_unavailable(
        self, conn, row_id, ticket_key, target_status, work_item_id, tenant_id, transitions, board
    ) -> None:
        """The board has no such status — settle the row for good.

        Marked `processed` (with the reason in `last_error`) rather than left to
        burn through `attempts`: the queue has no third state, and leaving it
        unprocessed is what made one missing column stall every transition queued
        behind it on the same ticket.

        The audit row carries both what the PROJECT has (so the operator can create
        exactly what is missing, or fix the configured column name) and what this
        card can reach right now, because a log line will have rotated away by the
        time anyone asks. Emitted exactly once: the row is settled in the same
        pass, and `enqueue_transition` dedups by `dedup_key`, so nothing re-opens
        it.

        The claim is deliberately the narrow one — "not a STATUS of this project's
        workflow". `_apply_transition` also accepts a match on a transition's name,
        and `/project/{key}/statuses` says nothing about names, so a target
        configured as a transition name rather than a status lands here. The
        recorded reason then reads exactly right and points at the configuration.
        """
        available_statuses = _to_statuses(transitions)
        available_names = sorted({(t.get("name") or "") for t in transitions} - {""})
        reason = f"unavailable_target_status: '{target_status}' is not a status of this project's workflow"
        logger.warning("%s (ticket=%s board_statuses=%s)", reason, ticket_key, sorted(board))
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE jira_transition_queue SET processed = true, processed_at = now(), last_error = %s WHERE id = %s",
                (reason[:500], row_id),
            )
        audit_emit(
            actor="system:adapter-jira",
            action="jira_transition_unavailable",
            tenant_id=tenant_id,
            work_item_id=work_item_id,
            details={
                "ticket_key": ticket_key,
                "target_status": target_status,
                "reason": "target_status_not_on_board",
                "board_statuses": sorted(board)[:_MAX_REPORTED_TRANSITIONS],
                "available_statuses": available_statuses[:_MAX_REPORTED_TRANSITIONS],
                "available_transitions": available_names[:_MAX_REPORTED_TRANSITIONS],
            },
        )
