"""A reply must be recoverable after it falls out of the poller's window.

The windowed sweep only sees issues updated in the last couple of minutes. When
an answer lands while the poller is blind — down, restarting, or simply reading
late — the ticket drops out of that window and is never looked at again. The task
then waits forever on a reply the platform is already displaying, in silence.

That is not hypothetical: BD-40 and BD-41 both needed a hand-written UPDATE on
`jira_poll_state` to move. This suite pins the behaviour that removes the need.
"""
from __future__ import annotations

from datetime import datetime, timezone

import adapter_jira.poller as poller_mod
from adapter_jira.backend import FakeJiraClient
from adapter_jira.poller import JiraPoller

NOW = datetime(2026, 7, 25, 21, 0, tzinfo=timezone.utc)
STALE_TICKET = "BD-41"


def _poller(client) -> JiraPoller:
    return JiraPoller(
        client,
        tenant_id="fintex-poc",
        projects=["BD"],
        trigger_label="dse",
        approved_status="Plan approved",
        rejected_status="Plan rejected",
    )


def _client_with_reply() -> FakeJiraClient:
    client = FakeJiraClient()
    # Deliberately NOT in issues_by_project: the windowed search must not return
    # it, which is exactly the situation being recovered from.
    client.comments[STALE_TICKET] = {"44625": "acceptance criteria: background must be yellow"}
    return client


def test_a_reply_outside_the_window_is_still_recovered(monkeypatch):
    client = _client_with_reply()
    monkeypatch.setattr(
        poller_mod,
        "pending_reply_work_items",
        lambda conn, **kw: [
            {"work_item_id": "wi_x", "source_ref": {"ticket_key": STALE_TICKET},
             "status": "needs_clarification"}
        ],
    )
    monkeypatch.setattr(poller_mod, "get_connection", lambda: _NullConn())
    seen = []
    monkeypatch.setattr(
        poller_mod, "ingest_comment",
        lambda **kw: seen.append((kw["key"], kw["comment_id"])) or {"ok": True},
    )

    _poller(client)._recover_pending_replies()

    assert seen == [(STALE_TICKET, "44625")], seen


def test_nothing_pending_means_no_platform_calls(monkeypatch):
    """The recovery runs every cycle, so with an empty queue it must cost
    nothing — otherwise it becomes a standing rate-limit risk."""
    client = _client_with_reply()
    monkeypatch.setattr(poller_mod, "pending_reply_work_items", lambda conn, **kw: [])
    monkeypatch.setattr(poller_mod, "get_connection", lambda: _NullConn())
    called = []
    monkeypatch.setattr(client, "get_comments", lambda key: called.append(key) or [])

    assert _poller(client)._recover_pending_replies() == 0
    assert called == []


def test_a_broken_ticket_does_not_stall_the_others(monkeypatch):
    """One malformed ticket must not strand every other blocked task — the same
    isolation rule the windowed sweep already follows."""
    client = _client_with_reply()
    monkeypatch.setattr(
        poller_mod,
        "pending_reply_work_items",
        lambda conn, **kw: [
            {"work_item_id": "wi_bad", "source_ref": {"ticket_key": "BD-BOOM"}, "status": "needs_clarification"},
            {"work_item_id": "wi_ok", "source_ref": {"ticket_key": STALE_TICKET}, "status": "needs_clarification"},
        ],
    )
    monkeypatch.setattr(poller_mod, "get_connection", lambda: _NullConn())

    def _get_comments(key):
        if key == "BD-BOOM":
            raise RuntimeError("malformed ADF")
        return [{"id": "44625", "body": "ok", "author": {"accountId": "a", "displayName": "A"}}]

    monkeypatch.setattr(client, "get_comments", _get_comments)
    monkeypatch.setattr(poller_mod, "ingest_comment", lambda **kw: {"ok": True})
    # resolve_principal reaches the database; stub it so this test exercises the
    # isolation rule rather than the environment's Postgres.
    monkeypatch.setattr(poller_mod, "resolve_principal", lambda *a, **k: "usr_stub")

    assert _poller(client)._recover_pending_replies() == 1


def test_a_work_item_without_a_ticket_key_is_skipped(monkeypatch):
    client = _client_with_reply()
    monkeypatch.setattr(
        poller_mod,
        "pending_reply_work_items",
        lambda conn, **kw: [{"work_item_id": "wi_x", "source_ref": {}, "status": "needs_clarification"}],
    )
    monkeypatch.setattr(poller_mod, "get_connection", lambda: _NullConn())
    assert _poller(client)._recover_pending_replies() == 0


def test_database_failure_does_not_break_the_sweep(monkeypatch):
    """Recovery is an addition to the sweep, never a new way for it to die."""
    def boom():
        raise RuntimeError("postgres down")

    monkeypatch.setattr(poller_mod, "get_connection", boom)
    assert _poller(_client_with_reply())._recover_pending_replies() == 0


class _NullConn:
    def close(self):
        pass


def test_a_reply_already_seen_costs_one_select_and_nothing_else(monkeypatch):
    """The steady state of this recovery is 'the same replies, again'.

    A task waiting on a human stays selected for as long as it waits, so every
    cycle re-reads the whole thread. Recording is idempotent, but the work
    BEFORE recording was not: correlation ran, steering was resolved, and
    `record_signal_event` wrote a `signal_duplicate_ignored` row. In production
    one ticket stuck in `needs_clarification` wrote ~2,900 audit rows in
    thirteen hours, into a log that is append-only and a console timeline that
    renders it.
    """
    import adapter_jira.ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "_is_dse_authored", lambda *a, **k: False)
    monkeypatch.setattr(ingest_mod, "get_connection", lambda: _NullConn())
    monkeypatch.setattr(ingest_mod, "recorded_work_item_id", lambda conn, event_id: "wi_prior")

    def _boom(*a, **k):
        raise AssertionError("a comment already ingested must not be correlated again")

    monkeypatch.setattr(ingest_mod, "correlate", _boom)
    monkeypatch.setattr(ingest_mod, "record_signal_event", _boom)

    out = ingest_mod.ingest_comment(
        tenant_id="fintex-poc", key=STALE_TICKET, comment_id="44625",
        body="acceptance criteria: background must be yellow",
        actor_account_id="acc-1", resolved_principal="usr_1",
    )
    assert out["path"] == "already_ingested" and out["work_item_id"] == "wi_prior"


def test_a_reply_never_seen_still_goes_all_the_way_through(monkeypatch):
    """The guard above must not swallow the case this recovery exists for."""
    import adapter_jira.ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "_is_dse_authored", lambda *a, **k: False)
    monkeypatch.setattr(ingest_mod, "get_connection", lambda: _NullConn())
    monkeypatch.setattr(ingest_mod, "recorded_work_item_id", lambda conn, event_id: None)

    class _Result:
        kind = "signal"
        work_item_id = "wi_x"

    monkeypatch.setattr(ingest_mod, "correlate", lambda *a, **k: _Result())
    recorded = []
    monkeypatch.setattr(ingest_mod, "record_signal_event", lambda ev, **kw: recorded.append(kw["work_item_id"]))

    out = ingest_mod.ingest_comment(
        tenant_id="fintex-poc", key=STALE_TICKET, comment_id="44625",
        body="acceptance criteria: background must be yellow",
        actor_account_id="acc-1", resolved_principal="usr_1",
    )
    assert out["path"] == "signal" and recorded == ["wi_x"]
