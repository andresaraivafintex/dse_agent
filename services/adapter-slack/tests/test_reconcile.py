"""Recovery of clarification replies whose webhook delivery was lost
(`POST /internal/reconcile`).

The failure being fixed is silent by construction: the human answers, Slack
shows their message, the delivery never lands, and the task stays in
`needs_clarification` forever with nobody looking at it. Observed twice in one
afternoon (BD-40, BD-41), both times unblocked by a hand-written UPDATE.

Two properties carry the security of this endpoint and are pinned below:
re-reading NEVER reconstructs an approval (a decision manufactured from text
nobody signed is exactly what the TOCTOU defense forbids), and re-reading NEVER
creates work (a thread that stopped correlating must not become one new task per
message).

Slack is the only thing faked here — the rest is the real handler. The
ingestion path itself is stubbed on purpose: what belongs to this suite is WHICH
messages reach it and WHAT is recorded about them; that the path works is
already proven end-to-end in test_inbound_flow.py.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import adapter_slack.app as app_module
from adapter_slack.app import RECONCILE_BUDGET_S, app
from adapter_slack.backend import FakeSlackClient
from adapter_slack.events import build_event_from_thread_message
from adapter_slack.ratelimit import SlackRateLimited

from .helpers import Clock

client = TestClient(app)

CHANNEL = "C_RECON"
THREAD_TS = "1784000000.000100"
REPLY_TS = "1784000900.000200"


def _work_item(work_item_id: str = "wi_stuck", *, channel: str = CHANNEL,
               thread_ts: str | None = THREAD_TS, status: str = "needs_clarification") -> dict:
    """Row shape returned by `pending_reply_work_items` (ingest_gateway)."""
    source_ref = {"channel": channel, "thread_ts": thread_ts} if thread_ts else {}
    return {"work_item_id": work_item_id, "source_ref": source_ref, "status": status}


def _human_reply(text: str = "use the staging database", *, ts: str = REPLY_TS,
                 user: str = "U_HUMAN") -> dict:
    return {"type": "message", "user": user, "ts": ts, "thread_ts": THREAD_TS, "text": text}


def _thread_root() -> dict:
    """The original task_request — already ingested when the task was created."""
    return {"type": "message", "user": "U_HUMAN", "ts": THREAD_TS, "text": "@dse fix the login bug"}


class _FakeConn:
    """The endpoint opens a connection only to LIST the blocked items; the
    listing itself is stubbed, so this needs nothing but the transaction
    boundary."""

    def __init__(self) -> None:
        self.closed = False

    def commit(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:
        self.closed = True


class _Pending:
    """Stands in for `pending_reply_work_items`, recording how it was asked.

    Which statuses are recoverable is the helper's own contract (proven in the
    ingest-gateway suite) — pinning it again here would only re-test the stub.
    What matters on this side is that the adapter DELEGATES the decision instead
    of writing its own status filter."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.calls: list[dict] = []

    def __call__(self, conn, *, tenant_id: str, source: str, limit: int = 50) -> list[dict]:
        self.calls.append({"tenant_id": tenant_id, "source": source, "limit": limit})
        return self.rows


class _Ingested:
    """Stands in for `_handle_conversation_event`, capturing every event that
    reaches the ingestion path. `results` queues the outcomes to hand back (a
    duplicate answers `recorded=False`, which is what dedup does in production)."""

    def __init__(self) -> None:
        self.events: list = []
        self.kwargs: list[dict] = []
        self.results: list[dict] = []

    def __call__(self, conv_event, **kwargs) -> dict:
        self.events.append(conv_event)
        self.kwargs.append(kwargs)
        if self.results:
            return self.results.pop(0)
        return {"ok": True, "path": "signal", "work_item_id": "wi_stuck", "recorded": True}


@pytest.fixture
def fake_slack(monkeypatch) -> FakeSlackClient:
    fake = FakeSlackClient()
    # `deadline` is required in production (see `build_real_slack_client`); the
    # fake never throttles, so it only has to accept it.
    monkeypatch.setattr(app_module, "build_real_slack_client", lambda token, *, deadline: fake)
    return fake


@pytest.fixture
def pending(monkeypatch) -> _Pending:
    stub = _Pending()
    monkeypatch.setattr(app_module, "get_connection", lambda: _FakeConn())
    monkeypatch.setattr(app_module, "pending_reply_work_items", stub)
    return stub


@pytest.fixture
def ingested(monkeypatch) -> _Ingested:
    stub = _Ingested()
    monkeypatch.setattr(app_module, "_handle_conversation_event", stub)
    return stub


@pytest.fixture(autouse=True)
def audits(monkeypatch) -> list[dict]:
    """Autouse: the audit ledger is a database write. Kept in memory here so a
    test that recovers something asserts on the trail instead of reaching for
    Postgres — the ledger's own guarantees live in the dse_audit suite."""
    rows: list[dict] = []
    monkeypatch.setattr(app_module, "audit_emit", lambda **kw: rows.append(kw))
    return rows


@pytest.fixture(autouse=True)
def _fake_identity(monkeypatch) -> None:
    """Autouse: `resolve_principal` self-registers in the identity map (a
    database round-trip). Only the SHAPE of the result matters to the
    reconciler — an `actor` is a resolved principal, never a raw Slack id."""
    monkeypatch.setattr(app_module, "resolve_principal", lambda platform, uid: f"usr_{uid.lower()}")


def _reconcile(body: dict | None = None) -> dict:
    resp = client.post("/internal/reconcile", json=body) if body else client.post("/internal/reconcile")
    assert resp.status_code == 200
    return resp.json()


# ---------------------------------------------------------------------------
# Message filter (pure)
# ---------------------------------------------------------------------------
def test_only_human_replies_below_the_root_are_recoverable():
    """Parity with the webhook: it ignores messages with no `user`, messages
    with a `subtype`, and never treats the root as a reply."""
    assert app_module._is_recoverable_reply(_human_reply(), THREAD_TS) is True
    assert app_module._is_recoverable_reply(_thread_root(), THREAD_TS) is False
    assert app_module._is_recoverable_reply(
        {"bot_id": "B_DSE", "user": "U_BOT", "ts": REPLY_TS, "text": "Which repository?"}, THREAD_TS
    ) is False
    assert app_module._is_recoverable_reply(
        {"ts": REPLY_TS, "text": "posted by an app"}, THREAD_TS  # no `user` at all
    ) is False
    assert app_module._is_recoverable_reply(
        {"subtype": "channel_join", "user": "U_HUMAN", "ts": REPLY_TS, "text": ""}, THREAD_TS
    ) is False


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------
def test_lost_reply_reaches_the_same_ingestion_path_as_a_webhook(pending, fake_slack, ingested, audits):
    pending.rows.append(_work_item())
    fake_slack.threads[f"{CHANNEL}:{THREAD_TS}"] = [_thread_root(), _human_reply()]

    assert _reconcile() == {"ok": True, "checked": 1, "recovered": 1}

    assert len(ingested.events) == 1
    event = ingested.events[0]
    assert event.kind.value == "clarification_answer"
    assert event.content_snapshot == "use the staging database"
    assert event.source_ref == {"channel": CHANNEL, "thread_ts": THREAD_TS}
    # The reconciler recovers replies; it must never admit work. Without this
    # leash a thread that stopped correlating becomes one NEW TASK per message.
    assert ingested.kwargs[0]["signal_only"] is True


def test_recovered_event_id_is_the_one_the_webhook_would_have_produced(pending, fake_slack, ingested, audits):
    """This is what makes re-reading safe to repeat: the id collides with the
    webhook's, so a reply that DID arrive dedupes on the UNIQUE constraint
    instead of being ingested twice."""
    pending.rows.append(_work_item())
    fake_slack.threads[f"{CHANNEL}:{THREAD_TS}"] = [_thread_root(), _human_reply()]

    _reconcile()

    from_webhook = build_event_from_thread_message(
        {"channel": CHANNEL, "ts": REPLY_TS, "thread_ts": THREAD_TS,
         "user": "U_HUMAN", "text": "use the staging database"},
        resolved_principal="usr_u_human",
    )
    assert ingested.events[0].event_id == from_webhook.event_id


def test_bot_messages_and_the_thread_root_are_never_recovered(pending, fake_slack, ingested, audits):
    """The bot's own question and the task's opening line are in the same thread.
    Feeding either back would answer the clarification with our own text."""
    pending.rows.append(_work_item())
    fake_slack.threads[f"{CHANNEL}:{THREAD_TS}"] = [
        _thread_root(),
        {"bot_id": "B_DSE", "ts": "1784000500.000150", "text": "Which repository should I use?"},
        {"subtype": "channel_join", "user": "U_OTHER", "ts": "1784000600.000160", "text": ""},
        _human_reply(),
    ]

    assert _reconcile()["recovered"] == 1
    assert [e.content_snapshot for e in ingested.events] == ["use the staging database"]


def test_every_recovered_event_is_auditable_as_recovered(pending, fake_slack, ingested, audits):
    """An event ingested from a re-read message is a different provenance claim
    than one from a signed webhook — the trail has to say so out loud."""
    pending.rows.append(_work_item("wi_stuck"))
    fake_slack.threads[f"{CHANNEL}:{THREAD_TS}"] = [_thread_root(), _human_reply()]

    _reconcile()

    recovered = [a for a in audits if a["action"] == "reply_recovered"]
    assert len(recovered) == 1
    assert recovered[0]["work_item_id"] == "wi_stuck"
    assert recovered[0]["details"]["message_ts"] == REPLY_TS
    assert recovered[0]["details"]["channel"] == CHANNEL
    # The sweep is the actor; the human only wrote the reply. `author` is a
    # resolved principal — never a raw Slack id (P8).
    assert recovered[0]["actor"] == "system:adapter-slack-reconciler"
    assert recovered[0]["details"]["author"] == "usr_u_human"


def test_a_reply_that_already_arrived_is_not_counted_or_audited_again(pending, fake_slack, ingested, audits):
    """Dedup on `event_id` does the work — the reconciler just has to stop
    claiming credit for it, or every cycle would re-audit the same reply."""
    pending.rows.append(_work_item())
    fake_slack.threads[f"{CHANNEL}:{THREAD_TS}"] = [_thread_root(), _human_reply()]
    ingested.results.append(
        {"ok": True, "path": "signal", "work_item_id": "wi_stuck", "recorded": False}
    )

    assert _reconcile() == {"ok": True, "checked": 1, "recovered": 0}
    assert len(ingested.events) == 1  # it was still re-submitted; dedup rejected it
    assert [a for a in audits if a["action"] == "reply_recovered"] == []


def test_a_reply_refused_by_the_steering_gate_is_not_a_recovery(pending, fake_slack, ingested, audits):
    """Recovery does not launder authorization: whoever could not steer through
    the webhook still cannot steer through the reconciler."""
    pending.rows.append(_work_item())
    fake_slack.threads[f"{CHANNEL}:{THREAD_TS}"] = [_thread_root(), _human_reply(user="U_STRANGER")]
    ingested.results.append({"ok": True, "path": "unauthorized"})

    assert _reconcile()["recovered"] == 0
    assert [a for a in audits if a["action"] == "reply_recovered"] == []


def test_one_broken_work_item_does_not_abort_the_sweep(pending, fake_slack, ingested, audits, monkeypatch):
    """The whole point is unblocking tasks nobody is watching. A single thread
    that cannot be read (deleted channel, Slack error) must not take the others
    down with it — and must not turn into a 5xx either."""
    pending.rows.extend([_work_item("wi_broken", channel="C_GONE"), _work_item("wi_ok")])
    fake_slack.threads[f"{CHANNEL}:{THREAD_TS}"] = [_thread_root(), _human_reply()]

    original = fake_slack.conversations_replies

    def _explode(*, channel: str, ts: str) -> dict:
        if channel == "C_GONE":
            raise RuntimeError("channel_not_found")
        return original(channel=channel, ts=ts)

    monkeypatch.setattr(fake_slack, "conversations_replies", _explode)

    assert _reconcile() == {"ok": True, "checked": 2, "recovered": 1}


def test_a_throttled_workspace_stops_the_sweep_instead_of_walking_into_it_again(
    pending, fake_slack, ingested, audits, monkeypatch
):
    """BLOCKER: the sweep shares ONE wait budget, so `SlackRateLimited` means the
    workspace is throttling `conversations.replies` — the very call every
    remaining item is about to make. Continuing was the bug: the per-item
    `except Exception` swallowed the throttle and marched on, so 50 items each
    burned their own budget inside a request the CronJob abandons after 120s."""
    pending.rows.extend([_work_item("wi_1"), _work_item("wi_2"), _work_item("wi_3")])
    fake_slack.threads[f"{CHANNEL}:{THREAD_TS}"] = [_thread_root(), _human_reply()]

    def _throttled(*, channel: str, ts: str) -> dict:
        fake_slack.replies_calls.append({"channel": channel, "ts": ts})
        raise SlackRateLimited("slack still rate limiting conversations.replies")

    monkeypatch.setattr(fake_slack, "conversations_replies", _throttled)

    assert _reconcile() == {"ok": True, "checked": 1, "recovered": 0}
    assert len(fake_slack.replies_calls) == 1  # item 2 and 3 were never asked for


def test_no_new_thread_is_started_once_the_sweep_is_out_of_budget(
    pending, fake_slack, ingested, audits, monkeypatch
):
    """The other half of the bound. Once the deadline passes, the client will not
    sleep any more — but it would still spend one Slack request per remaining
    item, which is what has to fit inside the CronJob's 120s. Here reading the
    first thread eats the whole budget, so the second item is never started."""
    clock = Clock()
    monkeypatch.setattr(app_module, "time", clock)
    pending.rows.extend([_work_item("wi_1"), _work_item("wi_2")])
    fake_slack.threads[f"{CHANNEL}:{THREAD_TS}"] = [_thread_root(), _human_reply()]

    original = fake_slack.conversations_replies

    def _slow(*, channel: str, ts: str) -> dict:
        clock.t += RECONCILE_BUDGET_S + 1.0  # this one call outlasted the budget
        return original(channel=channel, ts=ts)

    monkeypatch.setattr(fake_slack, "conversations_replies", _slow)

    assert _reconcile() == {"ok": True, "checked": 1, "recovered": 1}
    assert len(fake_slack.replies_calls) == 1


def test_work_item_without_a_thread_is_skipped_not_guessed(pending, fake_slack, ingested):
    """No channel/thread_ts means there is nothing to re-read. Reading the
    channel instead would pull in messages that were never a reply to anything."""
    pending.rows.append(_work_item(thread_ts=None))

    assert _reconcile() == {"ok": True, "checked": 1, "recovered": 0}
    assert fake_slack.replies_calls == []


def test_nothing_pending_never_touches_the_slack_api(pending, fake_slack, ingested):
    assert _reconcile() == {"ok": True, "checked": 0, "recovered": 0}
    assert fake_slack.replies_calls == []


def test_database_failure_reports_not_ok_instead_of_5xx(monkeypatch, fake_slack, ingested):
    """A reconciler on a timer that answers 500 just retries into the same
    failure; `ok: False` is the honest, quiet answer — and the same one the
    GitHub adapter's reconciler gives, so one caller reads both alike."""
    def _boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(app_module, "get_connection", _boom)

    resp = client.post("/internal/reconcile")

    assert resp.status_code == 200
    assert resp.json() == {"ok": False, "checked": 0, "recovered": 0}


def test_the_sweep_asks_only_for_slack_items_blocked_on_a_reply(pending, fake_slack, ingested):
    """The status filter is delegated to `pending_reply_work_items`, which
    excludes `awaiting_plan_approval` — the adapter never writes its own query
    and therefore cannot drift into re-reading an approval thread."""
    _reconcile({"tenant_id": "tenant_x", "limit": 7})

    assert pending.calls == [{"tenant_id": "tenant_x", "source": "slack", "limit": 7}]


def test_recovered_events_are_never_approvals(pending, fake_slack, ingested, audits):
    """SECURITY regression (TOCTOU / WSA-E2-T2): re-read text is a clarification
    answer and nothing else. Even a reply that literally says "approved" must not
    come back as an approval — an approval rebuilt from a re-read message is a
    decision nobody signed, and it can be edited after the fact."""
    pending.rows.append(_work_item(status="awaiting_repo_selection"))
    fake_slack.threads[f"{CHANNEL}:{THREAD_TS}"] = [
        _thread_root(),
        _human_reply(text="approved, go ahead"),
    ]

    _reconcile()

    assert [e.kind.value for e in ingested.events] == ["clarification_answer"]
    # no verdict marker is fabricated for the dispatcher to read
    assert "extra_payload" not in ingested.kwargs[0] or ingested.kwargs[0]["extra_payload"] is None
