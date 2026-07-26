"""A Jira board that does not have the target column must not stall the pipeline —
and a card that has merely not moved yet must not be mistaken for one.

Regression from the real board: no "Blocked" column existed, so five transitions
were treated as transient failures. They retried to exhaustion, and — worse —
because transitions are applied in order per ticket, each stuck row held back
every transition queued behind it on the same ticket. The work item's status in
Postgres is the system of record; the Jira column is only a mirror, and failing
to mirror must never stop the work.

The other half of the contract is the reason this file lists both boards:
`GET /issue/{key}/transitions` returns only the edges out of the issue's CURRENT
status, so an empty result is NOT evidence that the column is missing. Settling on
it discards a mirror update that would have worked once the card moved, and files
an audit row whose stated reason is false. The project's workflow statuses are
what answer that question.

No Postgres: `TransitionWorker` takes a `conn_factory`, so the queue table is
replaced by an in-memory stand-in that enforces the two flags the worker relies
on (`processed`, `attempts`). Audit rows are captured instead of written.
"""
from __future__ import annotations

import pytest

from adapter_jira.backend import FakeJiraClient, JiraRateLimited
from adapter_jira.transitions import _MAX_ATTEMPTS, TransitionWorker

from .helpers import FakeConn

TENANT = "test_tenant_jira_adapter"

BOARD_WITHOUT_BLOCKED = [
    {"id": "11", "name": "Start progress", "to_status": "In Progress"},
    {"id": "21", "name": "Ready for review", "to_status": "In Review"},
    {"id": "31", "name": "Close", "to_status": "Done"},
]

# The project's workflow — the configuration fact. Note that it contains statuses
# the transitions above cannot reach from the current column ("Blocked" is on this
# board), which is exactly the case the old code got wrong.
PROJECT_STATUSES = ["Backlog", "In Progress", "In Review", "Done", "Blocked"]


class FakeQueue:
    """In-memory `jira_transition_queue` — only the columns the worker reads and
    writes. Stateful on purpose: "audited exactly once" can only be proven by
    draining twice and seeing the settled row disappear."""

    def __init__(self, rows: list[dict]):
        self.rows = [
            {"processed": False, "attempts": 0, "last_error": None, "work_item_id": "wi_1", "tenant_id": TENANT, **r}
            for r in rows
        ]

    def _open(self) -> list[dict]:
        return [r for r in self.rows if not r["processed"] and r["attempts"] < _MAX_ATTEMPTS]

    def by_id(self, row_id: int) -> dict:
        return next(r for r in self.rows if r["id"] == row_id)

    def responder(self, sql: str, params):
        if sql.startswith("SELECT DISTINCT ticket_key"):
            seen: list[str] = []
            for r in self._open():
                if r["ticket_key"] not in seen:
                    seen.append(r["ticket_key"])
            return [(t,) for t in seen]
        if "pg_try_advisory_lock" in sql:
            return [(True,)]
        if "pg_advisory_unlock" in sql:
            return [(True,)]
        if sql.startswith("SELECT id, target_status, work_item_id, tenant_id"):
            rows = sorted((r for r in self._open() if r["ticket_key"] == params[0]), key=lambda r: r["id"])
            return [(r["id"], r["target_status"], r["work_item_id"], r["tenant_id"]) for r in rows]
        if sql.startswith("UPDATE jira_transition_queue SET attempts"):
            row = self.by_id(params[1])
            row["attempts"] += 1
            row["last_error"] = params[0]
            # RETURNING attempts: the worker settles the row once they run out,
            # so the fake has to answer with the new count.
            return [(row["attempts"],)]
        if sql.startswith("UPDATE jira_transition_queue SET processed"):
            row = self.by_id(params[-1])
            row["processed"] = True
            if len(params) == 2:
                row["last_error"] = params[0]
            return []
        raise AssertionError(f"unexpected statement: {sql}")


@pytest.fixture
def audit(monkeypatch) -> list[dict]:
    """The audit log is append-only and trigger-enforced; capturing the calls is
    both DB-free and the only way to assert "exactly one row"."""
    rows: list[dict] = []

    def _emit(**kwargs):
        rows.append(kwargs)

    monkeypatch.setattr("adapter_jira.transitions.audit_emit", _emit)
    return rows


def _worker(client, queue: FakeQueue) -> tuple[TransitionWorker, FakeConn]:
    conn = FakeConn(queue.responder)
    return TransitionWorker(client, conn_factory=lambda: conn), conn


def _board(*tickets: str, statuses: list[str] | None = None) -> FakeJiraClient:
    """A client whose issues offer BOARD_WITHOUT_BLOCKED and whose project has
    `statuses` (None = the project lookup does not answer)."""
    return FakeJiraClient(
        transitions_by_ticket={t: list(BOARD_WITHOUT_BLOCKED) for t in tickets},
        statuses_by_project=None if statuses is None else {"DSE": list(statuses)},
    )


def test_a_status_the_project_workflow_lacks_is_settled_instead_of_retried(audit):
    queue = FakeQueue([{"id": 1, "ticket_key": "DSE-200", "target_status": "Nonexistent"}])
    worker, _ = _worker(_board("DSE-200", statuses=PROJECT_STATUSES), queue)

    assert worker.drain_once() == 1  # settled: the row will never be seen again

    row = queue.by_id(1)
    assert row["processed"] is True
    assert row["attempts"] == 0  # NOT a failed attempt — nothing to retry
    assert "unavailable_target_status" in row["last_error"]
    assert [a["action"] for a in audit] == ["jira_transition_unavailable"]


def test_audit_row_tells_the_operator_what_to_create_on_the_board(audit):
    queue = FakeQueue([{"id": 1, "ticket_key": "DSE-200", "target_status": "Nonexistent"}])
    worker, _ = _worker(_board("DSE-200", statuses=PROJECT_STATUSES), queue)
    worker.drain_once()

    details = audit[0]["details"]
    assert details["target_status"] == "Nonexistent"
    assert details["reason"] == "target_status_not_on_board"
    # The operator's next question is always "then what does the project have?" —
    # and, separately, "what could this card reach?".
    assert details["board_statuses"] == ["Backlog", "Blocked", "Done", "In Progress", "In Review"]
    assert details["available_statuses"] == ["Done", "In Progress", "In Review"]
    assert details["available_transitions"] == ["Close", "Ready for review", "Start progress"]
    assert audit[0]["work_item_id"] == "wi_1"


def test_a_column_that_exists_but_is_not_reachable_yet_is_kept_not_discarded(audit):
    """The MEDIUM defect this file exists to pin down. "Blocked" IS on the board;
    the card simply cannot reach it from where it stands. Settling here throws away
    a mirror update that succeeds as soon as the card moves, under a reason that is
    not true."""
    queue = FakeQueue([{"id": 1, "ticket_key": "DSE-206", "target_status": "Blocked"}])
    worker, _ = _worker(_board("DSE-206", statuses=PROJECT_STATUSES), queue)

    assert worker.drain_once() == 0  # nothing settled

    row = queue.by_id(1)
    assert row["processed"] is False  # still there for when the card moves
    assert row["attempts"] == 1
    assert [a["action"] for a in audit] == ["jira_transition_not_reachable"]
    assert audit[0]["details"]["reason"] == "target_status_not_reachable_from_current"
    assert audit[0]["details"]["available_statuses"] == ["Done", "In Progress", "In Review"]


def test_it_applies_once_the_card_has_moved_somewhere_the_column_is_reachable(audit):
    """Same row, one drain later, after the board offers the edge."""
    queue = FakeQueue([{"id": 1, "ticket_key": "DSE-207", "target_status": "Blocked"}])
    client = _board("DSE-207", statuses=PROJECT_STATUSES)
    worker, _ = _worker(client, queue)

    assert worker.drain_once() == 0

    client.transitions_by_ticket["DSE-207"].append({"id": "41", "name": "Block", "to_status": "Blocked"})
    assert worker.drain_once() == 1
    assert queue.by_id(1)["processed"] is True
    assert [c["to_status"] for c in client.transition_calls] == ["Blocked"]


def test_an_unanswered_project_lookup_never_settles_the_row(audit):
    """`project_statuses` returning None means "could not ask", not "not there".
    Settling on it is the one outcome that loses a mirror update for good."""
    queue = FakeQueue([{"id": 1, "ticket_key": "DSE-208", "target_status": "Blocked"}])
    worker, _ = _worker(_board("DSE-208", statuses=None), queue)

    assert worker.drain_once() == 0
    assert queue.by_id(1)["processed"] is False
    assert audit[0]["action"] == "jira_transition_not_reachable"
    assert audit[0]["details"]["reason"] == "board_statuses_unknown"


def test_an_unreachable_column_is_abandoned_rather_than_retried_forever(audit):
    """Neither transient case may retry forever. The row used to just stop
    matching `attempts < _MAX_ATTEMPTS` and sit in the queue with nothing said
    about being given up on."""
    queue = FakeQueue([{"id": 1, "ticket_key": "DSE-209", "target_status": "Blocked"}])
    worker, _ = _worker(_board("DSE-209", statuses=PROJECT_STATUSES), queue)

    for _ in range(_MAX_ATTEMPTS + 10):
        worker.drain_once()

    row = queue.by_id(1)
    assert row["attempts"] == _MAX_ATTEMPTS
    assert row["processed"] is True  # settled, and visibly so
    assert "abandoned_after_" in row["last_error"]
    actions = [a["action"] for a in audit]
    assert actions == ["jira_transition_not_reachable"] * (_MAX_ATTEMPTS - 1) + ["jira_transition_abandoned"]
    assert audit[-1]["details"]["attempts"] == _MAX_ATTEMPTS


def test_a_missing_column_does_not_hold_back_the_transitions_behind_it(audit):
    """The expensive half of the original bug: transitions are ordered per
    ticket, so one unsettleable row froze the whole ticket's queue."""
    queue = FakeQueue(
        [
            {"id": 1, "ticket_key": "DSE-201", "target_status": "Nonexistent"},
            {"id": 2, "ticket_key": "DSE-201", "target_status": "In Review"},
        ]
    )
    client = _board("DSE-201", statuses=PROJECT_STATUSES)
    worker, _ = _worker(client, queue)

    assert worker.drain_once() == 2
    assert queue.by_id(1)["processed"] is True
    assert queue.by_id(2)["processed"] is True
    assert [c["to_status"] for c in client.transition_calls] == ["In Review"]
    assert [a["action"] for a in audit] == ["jira_transition_unavailable", "jira_transition_applied"]


def test_the_configuration_fact_is_audited_exactly_once(audit):
    """The queue drains once a cycle. An unavailable transition that stayed
    unprocessed would write one audit row per cycle, forever, into an
    append-only ledger — the failure mode that once produced ~2,900 rows."""
    queue = FakeQueue([{"id": 1, "ticket_key": "DSE-202", "target_status": "Nonexistent"}])
    worker, _ = _worker(_board("DSE-202", statuses=PROJECT_STATUSES), queue)

    worker.drain_once()
    assert worker.drain_once() == 0  # nothing left to see
    assert worker.drain_once() == 0

    assert [a["action"] for a in audit] == ["jira_transition_unavailable"]


def test_a_transient_failure_is_still_retried_and_still_blocks_its_ticket(audit):
    """The other half of the contract: this change must not turn genuine
    infrastructure failures into silently dropped transitions."""

    class Broken(FakeJiraClient):
        def get_transitions(self, key):
            raise RuntimeError("jira 503")

    queue = FakeQueue(
        [
            {"id": 1, "ticket_key": "DSE-203", "target_status": "In Progress"},
            {"id": 2, "ticket_key": "DSE-203", "target_status": "In Review"},
        ]
    )
    worker, _ = _worker(Broken(), queue)

    assert worker.drain_once() == 0
    assert queue.by_id(1)["processed"] is False  # kept for the next drain
    assert queue.by_id(1)["attempts"] == 1
    assert queue.by_id(2)["attempts"] == 0  # ordering preserved: never skipped ahead
    assert [a["action"] for a in audit] == ["jira_transition_failed"]


def test_rate_limiting_is_transient_not_a_board_misconfiguration(audit):
    """A 429 while reading the workflow says nothing about which columns exist.
    Settling the row on it would drop the mirror update for good."""

    class RateLimited(FakeJiraClient):
        def get_transitions(self, key):
            raise JiraRateLimited("GET", f"/rest/api/3/issue/{key}/transitions", retries=5, retry_after=None)

    queue = FakeQueue([{"id": 1, "ticket_key": "DSE-204", "target_status": "In Review"}])
    worker, _ = _worker(RateLimited(), queue)

    assert worker.drain_once() == 0
    assert queue.by_id(1)["processed"] is False
    assert queue.by_id(1)["attempts"] == 1
    assert [a["action"] for a in audit] == ["jira_transition_failed"]


def test_a_client_whose_project_lookup_blows_up_keeps_the_row(audit):
    """The lookup only classifies a failure. It must not be able to kill the drain
    loop, and it must not be read as "the column does not exist" either."""

    class Broken(FakeJiraClient):
        def project_statuses(self, project_key):
            raise RuntimeError("jira 500")

    queue = FakeQueue([{"id": 1, "ticket_key": "DSE-210", "target_status": "Blocked"}])
    worker, _ = _worker(Broken(transitions_by_ticket={"DSE-210": list(BOARD_WITHOUT_BLOCKED)}), queue)

    assert worker.drain_once() == 0
    assert queue.by_id(1)["processed"] is False
    assert audit[0]["details"]["reason"] == "board_statuses_unknown"


def test_a_transition_that_exists_still_applies(audit):
    queue = FakeQueue([{"id": 1, "ticket_key": "DSE-205", "target_status": "In Progress"}])
    client = _board("DSE-205", statuses=PROJECT_STATUSES)
    worker, _ = _worker(client, queue)

    assert worker.drain_once() == 1
    assert client.status_by_ticket["DSE-205"] == "In Progress"
    assert queue.by_id(1)["last_error"] is None
    assert [a["action"] for a in audit] == ["jira_transition_applied"]
