"""Reply reconciler (`POST /internal/reconcile`) — recovery of clarification
answers whose webhook delivery was lost.

Unlike the rest of this suite, these tests stub the database boundary
(`get_connection`, `pending_reply_work_items`, `resolve_principal`, the two
correlate/ingest handlers, `audit_emit`) instead of using a real Postgres. What
is under test here is the reconciler's own decisions — which threads it reads,
which comments it refuses to ingest, how failures are isolated, what it audits —
and every one of them is a decision made BEFORE any row is written. Stubbing the
handlers is also what lets a test assert what the reconciler HANDS to the
intake, which is the only way to prove it never manufactures an approval.

The thread itself is the documented `FakeGithubClient`, seeded with objects
shaped like the real API responses; the routing, the builder and the bot filter
are 100% real.
"""
from __future__ import annotations

import pytest
from dse_contracts import EventKind
from fastapi.testclient import TestClient

import adapter_github.app as app_module
from adapter_github.app import RECONCILE_BUDGET_S, app
from adapter_github.backend import FakeGithubClient
from adapter_github.ratelimit import GithubRateLimited

from .helpers import Clock

client = TestClient(app)

TENANT_ID = "test_tenant_github_adapter"
REPO = "acme/widgets"


@pytest.fixture(autouse=True)
def _cleanup():
    """Overrides the package-wide Postgres cleanup from conftest.py.

    These tests never open a connection, so there is nothing to delete — and
    requiring a live database to tear down what was never written would be the
    only reason this file could not run.
    """
    yield


def _comment(comment_id: int, body: str, login: str, user_type: str = "User") -> dict:
    """A comment object with the fields the intake reads, as the API returns
    them (`user.type` is what tells a GitHub App's comment apart from a human's)."""
    return {"id": comment_id, "body": body, "user": {"login": login, "type": user_type}}


class _IngestSpy:
    """Stands in for the correlate/record_signal_event handlers.

    Records the ConversationEvent it was handed and replies the way the real
    handler would for an active work item: a freshly recorded signal.
    """

    def __init__(self, *, recorded: bool = True, work_item_id: str = "wi_stuck"):
        self.events: list = []
        self.calls: list[dict] = []
        self._recorded = recorded
        self._work_item_id = work_item_id

    def __call__(self, conv_event, *, principal: str, tenant_id: str, **kwargs):
        self.events.append(conv_event)
        self.calls.append({"principal": principal, "tenant_id": tenant_id, **kwargs})
        return {"ok": True, "path": "signal", "work_item_id": self._work_item_id,
                "recorded": self._recorded}


#: kwargs of the last `pending_reply_work_items` call — that helper is what
#: decides WHICH states may be recovered, so the reconciler must be shown to
#: delegate the choice to it rather than filtering on its own.
captured_query: dict = {}


def _wire(monkeypatch, *, pending: list[dict], fake: FakeGithubClient,
          issue_handler=None, pr_handler=None) -> list[dict]:
    """Wires the endpoint against fixtures and returns the captured audit rows."""
    audit_rows: list[dict] = []
    captured_query.clear()

    class _FakeConn:
        def close(self) -> None:
            pass

    def _pending_reply_work_items(conn, **kwargs):
        captured_query.update(kwargs)
        return pending

    monkeypatch.setattr(app_module, "get_connection", lambda: _FakeConn())
    monkeypatch.setattr(app_module, "pending_reply_work_items", _pending_reply_work_items)
    # `deadline` is required in production (see `build_real_github_client`); the
    # fake never throttles, so it only has to accept it.
    monkeypatch.setattr(app_module, "build_real_github_client", lambda *, deadline: fake)
    monkeypatch.setattr(app_module, "resolve_principal", lambda platform, uid, name=None: f"usr_{uid}")
    monkeypatch.setattr(app_module, "_handle_task_creating_event", issue_handler or _IngestSpy())
    monkeypatch.setattr(app_module, "_handle_pr_comment_event", pr_handler or _IngestSpy())
    monkeypatch.setattr(app_module, "audit_emit", lambda **kw: audit_rows.append(kw))
    return audit_rows


def _pending(work_item_id: str, number: int, status: str = "needs_clarification") -> dict:
    return {"work_item_id": work_item_id, "source_ref": {"repo": REPO, "number": number},
            "status": status}


def test_lost_reply_on_a_blocked_issue_is_ingested_and_audited(monkeypatch):
    """The whole point: the human answered, the delivery was lost, and the
    reconciler puts that answer through the normal intake."""
    fake = FakeGithubClient()
    fake.seed_issue(REPO, 42, comments=[_comment(9001, "use postgres, not mysql", "alice")])
    spy = _IngestSpy(work_item_id="wi_stuck_42")
    audit_rows = _wire(monkeypatch, pending=[_pending("wi_stuck_42", 42)], fake=fake, issue_handler=spy)

    data = client.post("/internal/reconcile").json()

    assert data == {"ok": True, "checked": 1, "recovered": 1}
    assert len(spy.events) == 1
    assert spy.events[0].content_snapshot == "use postgres, not mysql"
    assert spy.events[0].source_ref == {"repo": REPO, "number": 42}

    recovered = [r for r in audit_rows if r["action"] == "reply_recovered"]
    assert len(recovered) == 1
    assert recovered[0]["work_item_id"] == "wi_stuck_42"
    assert recovered[0]["details"]["comment_id"] == 9001
    assert recovered[0]["details"]["repo"] == REPO
    assert recovered[0]["actor"] == "system:adapter-github-reconciler"


def test_only_recoverable_states_are_asked_for(monkeypatch):
    """The reconciler never picks the work items itself: it asks the helper,
    which returns replies-only and excludes `awaiting_plan_approval`."""
    _wire(monkeypatch, pending=[], fake=FakeGithubClient())

    client.post("/internal/reconcile")

    assert captured_query["source"] == "github"
    assert captured_query["tenant_id"] == TENANT_ID


def test_recovered_reply_is_never_an_approval(monkeypatch):
    """TOCTOU (WSA-E2-T2): re-read text may become a clarification answer, never
    an approval. A comment that says 'approved' is still just a comment."""
    fake = FakeGithubClient()
    fake.seed_issue(REPO, 55, comments=[_comment(9002, "approved, ship it", "alice")])
    spy = _IngestSpy()
    _wire(monkeypatch, pending=[_pending("wi_stuck_55", 55)], fake=fake, issue_handler=spy)

    client.post("/internal/reconcile")

    assert spy.events[0].kind is EventKind.clarification_answer
    assert spy.events[0].kind is not EventKind.approval


def test_a_recovered_mention_stays_a_reply_and_is_not_promoted(monkeypatch):
    """Mentioning the bot is how people phrase a reply on GitHub, so a recovered
    comment must NOT be promoted to `task_request`.

    Promotion makes the reply undeliverable: the dispatcher matches on kind
    before routing signals, calls start_workflow, gets WorkflowAlreadyStarted,
    and marks the row deduped — the waiting workflow never sees the answer. The
    reconciler would still count it as recovered and write `reply_recovered` to
    the ledger, so the mechanism built to expose a silent failure would be
    testifying that it had been fixed. Left as a clarification answer, it routes
    and actually unblocks the task."""
    fake = FakeGithubClient()
    fake.seed_issue(REPO, 65, comments=[_comment(9105, "@dse-bot use the v2 endpoint", "alice")])
    spy = _IngestSpy()
    _wire(monkeypatch, pending=[_pending("wi_stuck_65", 65)], fake=fake, issue_handler=spy)

    client.post("/internal/reconcile")

    assert spy.events[0].kind is EventKind.clarification_answer
    assert spy.events[0].kind is not EventKind.task_request


def test_recovery_never_opens_a_new_work_item(monkeypatch):
    """The guard the adversarial review found missing.

    A re-read thread can stop correlating — the item raced into a terminal
    status, or a newer item on the same thread is already done. Without
    `signal_only` every message in that thread falls through to
    `admit_work_item`: one new WorkItem and one real agent turn per comment,
    from text nobody just sent. This test drives the REAL handler rather than a
    spy, because a spy is exactly what hid the defect the first time."""
    import adapter_github.app as app_module

    fake = FakeGithubClient()
    fake.seed_issue(REPO, 66, comments=[
        _comment(9106, "@dse-bot first", "alice"),
        _comment(9107, "@dse-bot second", "alice"),
    ])
    _wire(monkeypatch, pending=[_pending("wi_dead_66", 66)], fake=fake)

    admitted = []
    monkeypatch.setattr(app_module, "admit_work_item",
                        lambda *a, **k: admitted.append(1) or "wi_new")
    # correlate no longer recognises the thread — the terminal-status case.
    class _NewTask:
        kind = "new_task"
        work_item_id = None
        provenance_work_item_id = None

    monkeypatch.setattr(app_module, "correlate", lambda *a, **k: _NewTask())
    monkeypatch.setattr(app_module, "record_signal_event", lambda *a, **k: True)

    resp = client.post("/internal/reconcile")

    assert resp.status_code == 200
    assert admitted == [], "the reconciler opened work from re-read text"


def test_bot_comments_are_not_ingested(monkeypatch):
    """The DSE's own status/question comment must not come back as the human's
    answer — that feedback loop is what broke BD-40 on the Jira poller."""
    fake = FakeGithubClient()
    fake.seed_issue(REPO, 60, comments=[
        _comment(9101, "I need acceptance criteria", "dse-bot[bot]", user_type="Bot"),
        _comment(9102, "status: running L1", "some-app[bot]"),
        _comment(9103, "status under a plain user account", "dse-bot"),
        _comment(9104, "here are the criteria", "alice"),
    ])
    spy = _IngestSpy()
    audit_rows = _wire(monkeypatch, pending=[_pending("wi_stuck_60", 60)], fake=fake, issue_handler=spy)

    data = client.post("/internal/reconcile").json()

    assert [e.content_snapshot for e in spy.events] == ["here are the criteria"]
    assert data["recovered"] == 1
    assert len([r for r in audit_rows if r["action"] == "reply_recovered"]) == 1


def test_comment_on_a_pull_request_keeps_the_pr_path(monkeypatch):
    """A PR comment goes through the handler that can never open a WorkItem
    (WSA-E4-T1) — the reconciler preserves that split instead of flattening it."""
    fake = FakeGithubClient()
    fake.seed_issue(REPO, 70, is_pull_request=True, comments=[_comment(9201, "rename this", "carol")])
    issue_spy, pr_spy = _IngestSpy(), _IngestSpy()
    _wire(monkeypatch, pending=[_pending("wi_stuck_70", 70)], fake=fake,
          issue_handler=issue_spy, pr_handler=pr_spy)

    client.post("/internal/reconcile")

    assert len(issue_spy.events) == 0
    assert len(pr_spy.events) == 1
    assert pr_spy.events[0].kind is EventKind.review_comment


def test_already_ingested_reply_recovers_nothing_and_audits_nothing(monkeypatch):
    """Re-reading a thread is idempotent by dedup on `event_id`: the second
    sweep must not claim a recovery nor write a second audit row."""
    fake = FakeGithubClient()
    fake.seed_issue(REPO, 80, comments=[_comment(9301, "already delivered", "alice")])
    spy = _IngestSpy(recorded=False)
    audit_rows = _wire(monkeypatch, pending=[_pending("wi_stuck_80", 80)], fake=fake, issue_handler=spy)

    data = client.post("/internal/reconcile").json()

    assert data == {"ok": True, "checked": 1, "recovered": 0}
    assert len(spy.events) == 1  # it still went through the idempotent intake
    assert [r for r in audit_rows if r["action"] == "reply_recovered"] == []


def test_one_unreadable_thread_does_not_cost_the_others_their_recovery(monkeypatch):
    """The failure this whole feature exists to prevent is a silent one — an
    unreadable thread aborting the sweep would recreate it for every item behind
    it in the list."""
    fake = FakeGithubClient()
    # 91 is never seeded -> get_issue raises, like a 404 on a deleted issue.
    fake.seed_issue(REPO, 92, comments=[_comment(9401, "the answer", "alice")])
    spy = _IngestSpy()
    _wire(monkeypatch, pending=[_pending("wi_gone_91", 91), _pending("wi_stuck_92", 92)],
          fake=fake, issue_handler=spy)

    data = client.post("/internal/reconcile").json()

    assert data == {"ok": True, "checked": 2, "recovered": 1}
    assert [e.content_snapshot for e in spy.events] == ["the answer"]


def test_a_throttled_installation_stops_the_sweep_instead_of_walking_into_it_again(monkeypatch):
    """BLOCKER: the sweep shares ONE wait budget, so `GithubRateLimited` means the
    installation is throttling — the very quota every remaining thread is about to
    spend. Continuing was the bug: the per-item `except Exception` swallowed the
    throttle and marched on, so each thread burned its own budget (and up to five
    pages of it) inside a request the CronJob abandons after 120s."""
    fake = FakeGithubClient()
    for number in (101, 102, 103):
        fake.seed_issue(REPO, number, comments=[_comment(9500 + number, "the answer", "alice")])
    _wire(monkeypatch, pending=[_pending(f"wi_{n}", n) for n in (101, 102, 103)], fake=fake)

    def _throttled(repo: str, number: int) -> dict:
        fake.get_issue_calls.append({"repo": repo, "number": number})
        raise GithubRateLimited("github still rate limiting get_issue")

    monkeypatch.setattr(fake, "get_issue", _throttled)

    data = client.post("/internal/reconcile").json()

    assert data == {"ok": True, "checked": 1, "recovered": 0}
    assert len(fake.get_issue_calls) == 1  # threads 102 and 103 were never read


def test_no_new_thread_is_started_once_the_sweep_is_out_of_budget(monkeypatch):
    """The other half of the bound. Once the deadline passes the client will not
    sleep any more — but it would still spend `get_issue` plus up to five comment
    pages per remaining thread, and that is what has to fit inside the CronJob's
    120s. Here reading the first thread eats the whole budget, so the second is
    never started."""
    clock = Clock()
    monkeypatch.setattr(app_module, "time", clock)
    fake = FakeGithubClient()
    for number in (111, 112):
        fake.seed_issue(REPO, number, comments=[_comment(9600 + number, "the answer", "alice")])
    _wire(monkeypatch, pending=[_pending(f"wi_{n}", n) for n in (111, 112)], fake=fake)

    original = fake.get_issue

    def _slow(repo: str, number: int) -> dict:
        clock.t += RECONCILE_BUDGET_S + 1.0  # this one thread outlasted the budget
        return original(repo, number)

    monkeypatch.setattr(fake, "get_issue", _slow)

    data = client.post("/internal/reconcile").json()

    assert data == {"ok": True, "checked": 1, "recovered": 1}
    assert len(fake.get_issue_calls) == 1


def test_malformed_source_ref_is_skipped_without_5xx(monkeypatch):
    fake = FakeGithubClient()
    _wire(monkeypatch, pending=[{"work_item_id": "wi_bad", "source_ref": {}, "status": "needs_clarification"}],
          fake=fake)

    resp = client.post("/internal/reconcile")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "checked": 1, "recovered": 0}
    assert fake.get_issue_calls == []


def test_nothing_pending_does_not_touch_github(monkeypatch):
    """The common case is an empty list; it must not cost a GitHub App token
    exchange on every tick of the timer."""
    built: list[int] = []

    def _build(*, deadline):
        built.append(1)
        return FakeGithubClient()

    _wire(monkeypatch, pending=[], fake=FakeGithubClient())
    monkeypatch.setattr(app_module, "build_real_github_client", _build)

    data = client.post("/internal/reconcile").json()

    assert data == {"ok": True, "checked": 0, "recovered": 0}
    assert built == []


def test_database_failure_reports_not_ok_instead_of_5xx(monkeypatch):
    """A reconciler on a timer that answers 500 just retries into the same
    failure; `ok: False` is the honest, quiet answer."""
    def _boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(app_module, "get_connection", _boom)

    resp = client.post("/internal/reconcile")

    assert resp.status_code == 200
    assert resp.json() == {"ok": False, "checked": 0, "recovered": 0}
