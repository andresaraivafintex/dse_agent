"""The DSE must ignore its OWN Jira comment — and nobody else's.

Two real failures, one after the other, define this behaviour:

BD-40: the DSE asked "I need acceptance criteria", the poller read that comment
back as the human's ANSWER, the criteria became the question itself, the Coder
changed nothing, the Tester correctly failed a test asserting the change, and
the run died at the retry cap.

BD-41: the first fix filtered by AUTHOR — and Jira attributes the DSE's comments
to whoever owns the API token, which is a real person. So the filter silenced
that person's reply too, and the task could never be unblocked by the one human
who cared about it.

Hence the test here is IDENTITY, not authorship: the writer records the id of
the comment it created, so the DSE recognises its own words exactly. It holds
whether the token belongs to a person or to a dedicated bot account.
"""
from __future__ import annotations

import adapter_jira.ingest as ingest

DSE_COMMENT_ID = "44624"
HUMAN_COMMENT_ID = "44625"
TICKET = "BD-41"


class _FakeCursor:
    def __init__(self, rows_for):
        self._rows_for = rows_for
        self._row = None

    def execute(self, _sql, params):
        _surface, comment_id, ticket_key = params
        self._row = (1,) if (comment_id, ticket_key) in self._rows_for else None

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, rows_for):
        self._rows_for = rows_for
        self.closed = False

    def cursor(self):
        return _FakeCursor(self._rows_for)

    def close(self):
        self.closed = True


def _with_recorded(monkeypatch, rows_for):
    conn = _FakeConn(rows_for)
    monkeypatch.setattr(ingest, "get_connection", lambda: conn)
    return conn


def test_the_comment_the_dse_wrote_is_recognised(monkeypatch):
    _with_recorded(monkeypatch, {(DSE_COMMENT_ID, TICKET)})
    assert ingest._is_dse_authored(TICKET, DSE_COMMENT_ID) is True


def test_a_human_reply_on_the_same_ticket_is_not(monkeypatch):
    """The regression from BD-41. Same ticket, same Jira account, different
    comment — it must get through."""
    _with_recorded(monkeypatch, {(DSE_COMMENT_ID, TICKET)})
    assert ingest._is_dse_authored(TICKET, HUMAN_COMMENT_ID) is False


def test_the_same_comment_id_on_another_ticket_is_not(monkeypatch):
    """Comment ids are unique per Jira site, but the ticket is matched too so a
    stale or mismatched ref can never silence an unrelated issue."""
    _with_recorded(monkeypatch, {(DSE_COMMENT_ID, TICKET)})
    assert ingest._is_dse_authored("BD-99", DSE_COMMENT_ID) is False


def test_connection_failure_fails_open(monkeypatch):
    """If the lookup cannot run, letting a comment through risks the old loop;
    blocking it risks swallowing the human's answer. The loop is recoverable —
    a swallowed reply looks exactly like a dead system."""
    def boom():
        raise RuntimeError("postgres down")

    monkeypatch.setattr(ingest, "get_connection", boom)
    assert ingest._is_dse_authored(TICKET, DSE_COMMENT_ID) is False


def test_connection_is_always_closed(monkeypatch):
    conn = _with_recorded(monkeypatch, {(DSE_COMMENT_ID, TICKET)})
    ingest._is_dse_authored(TICKET, DSE_COMMENT_ID)
    assert conn.closed is True
