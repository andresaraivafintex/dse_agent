"""`update_work_item_status` must project EVERY status, including out of a
terminal one — because an ordinary retry reuses the same work item row.

THE REGRESSION THESE TESTS EXIST FOR. A guard was added to this Activity that
refused to move the row out of `done|failed|blocked|escalated`, on the reasoning
that a retry is always a NEW row. It is not: the requester comments `@dse-bot` on
the issue of an `escalated` item, adapter-github promotes that comment to
`kind=task_request` (`adapter_github/app.py`), and `ingest_gateway.correlate`
matches the EXISTING row, since its own terminal set is only `done|failed`. The
dispatcher then starts the lifecycle workflow again under the same
`work_item_id`, so every status the retry writes went through the guard and was
refused: the projection stayed frozen at `escalated` for the whole retry. That is
not cosmetic — `ingest_gateway.dispatcher._route_signal` picks the plan-approval
signal by reading `work_items.status`, so a frozen row makes the dispatcher
DECLINE the human's approval (`DECLINED_UNEXPECTED_STATUS`) while the agent keeps
running, and the retry can never be approved.

WHAT THESE TESTS CAN AND CANNOT SEE. There is no Postgres here, so they read the
statement the Activity builds and the shape of its result — enough to catch the
guard coming back, since the guard had to make the statement depend on the
requested status. Whether that statement does the right thing to a real row is
covered by the DB-backed suites (`test_lifecycle_happy_path.py` and friends).
"""
from __future__ import annotations

from typing import Any

import pytest

from dse_orchestrator import local_activities
from dse_orchestrator.local_activities import update_work_item_status


@pytest.fixture(autouse=True)
def _require_postgres():
    """Overrides conftest's autouse skip: the connection is faked below, so this
    module must RUN where the foundation infra is absent."""
    yield


class _RecordingCursor:
    def __init__(self, row):
        self._row = row
        self.executed: list[tuple[str, Any]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._row


class _FakeConnection:
    def __init__(self, row):
        self.cursor_obj = _RecordingCursor(row)
        self.commits = 0
        self.closed = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed += 1


def _install(monkeypatch, *, row):
    conn = _FakeConnection(row)
    monkeypatch.setattr(local_activities, "_get_connection", lambda: conn)
    return conn


def _row(status: str, *, state_version: int = 7):
    # RETURNING status, state_version, plan_hash, base_sha, head_sha, ci_status
    return (status, state_version, None, None, None, None)


@pytest.mark.asyncio
async def test_a_retry_of_an_escalated_item_still_projects_its_status(monkeypatch):
    """The exact write the guard broke: the retry of an `escalated` item reaches
    the plan gate and writes `awaiting_plan_approval` on the SAME row.

    The SET clause has to be the plain COALESCE, with nothing in the statement
    that reads the current value to decide — that condition is what froze the
    row, and the frozen row is what made the dispatcher decline the approval.
    """
    conn = _install(monkeypatch, row=_row("awaiting_plan_approval", state_version=8))
    result = await update_work_item_status(
        {"work_item_id": "wi_1", "status": "awaiting_plan_approval"}
    )
    sql, params = conn.cursor_obj.executed[0]
    assert "status = COALESCE(%s, status)" in sql
    # No branch on the row's own status anywhere in the statement. `IS DISTINCT
    # FROM` (state_version/last_transition_at) reads it to detect a no-op, and
    # never to refuse the write.
    assert "terminal" not in sql
    assert "ANY(" not in sql
    assert result["persisted"] is True
    assert result["status"] == "awaiting_plan_approval"
    # `status_frozen` was the guard's report channel; nothing consumes it now and
    # its presence would mean a refusal path is back.
    assert "status_frozen" not in result
    assert conn.commits == 1 and conn.closed == 1


@pytest.mark.asyncio
async def test_the_statement_never_depends_on_the_requested_status(monkeypatch):
    """One statement for every caller.

    The guard could only be expressed by building a DIFFERENT statement when the
    requested status was non-terminal, so a single SQL string across the whole
    status vocabulary is the property that keeps it out. It also removes the
    class of bug that shape invites: two SQL variants, one of them rarely
    exercised.
    """
    statements = set()
    for status in ("implementing", "awaiting_plan_approval", "queued", "done",
                   "escalated", "blocked", "failed", None):
        conn = _install(monkeypatch, row=_row("escalated"))
        await update_work_item_status({"work_item_id": "wi_1", "status": status})
        statements.add(conn.cursor_obj.executed[0][0])
    assert len(statements) == 1, "the status write is conditional on the target status"


@pytest.mark.asyncio
async def test_absent_row_still_returns_not_persisted(monkeypatch):
    """Unchanged behaviour (isolated tests run without the work_items row)."""
    _install(monkeypatch, row=None)
    assert await update_work_item_status(
        {"work_item_id": "wi_missing", "status": "implementing"}
    ) == {"persisted": False}
