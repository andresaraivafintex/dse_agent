"""429 handling in the Jira HTTP client (`RealJiraClient._request`).

Jira Cloud budgets requests per ACCOUNT, and every DSE component that talks to
Jira shares one — poller, transition worker and status-comment writer. So a 429
is routine, and before this it was indistinguishable from a real error: the
transition worker spent one of its five attempts on it and the poller abandoned
the issue for that sweep.

No Postgres and no network: the transport is a fake session and `sleep` is
injected, so the schedule is asserted without spending the time it describes.
"""
from __future__ import annotations

from email.utils import format_datetime
from datetime import datetime, timedelta, timezone

import pytest
import requests

from adapter_jira.backend import (
    _RATE_LIMIT_BUDGET_WINDOW,
    _RATE_LIMIT_MAX_RETRIES,
    _RATE_LIMIT_TOTAL_BUDGET,
    JiraRateLimited,
    RealJiraClient,
)


class FakeResponse:
    def __init__(self, status_code: int, *, headers: dict | None = None, payload=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} for url", response=self)

    def json(self):
        return self._payload


class FakeSession:
    """Replays `responses` in order; the last one repeats forever, which is how
    "Jira is rate limiting and does not stop" is expressed."""

    def __init__(self, responses: list[FakeResponse]):
        self._responses = responses
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self._responses[min(len(self.calls) - 1, len(self._responses) - 1)]


class CyclingSession(FakeSession):
    """Replays `responses` round and round, so a SEQUENCE of requests can each be
    rate limited once and then succeed — the shape of a sweep, which is where the
    budget has to hold."""

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self._responses[(len(self.calls) - 1) % len(self._responses)]


class FakeClock:
    """Monotonic clock the test advances by hand: the budget window is 60 seconds
    of real time, and no test may spend it."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _client(responses: list[FakeResponse], *, session=None, clock=None) -> tuple[RealJiraClient, FakeSession, list[float]]:
    session = session or FakeSession(responses)
    sleeps: list[float] = []
    client = RealJiraClient(
        "https://acme.atlassian.net/",
        "bot@example.com",
        "token",
        session=session,
        sleep=sleeps.append,
        **({"clock": clock} if clock else {}),
    )
    return client, session, sleeps


def _ok(payload=None) -> FakeResponse:
    return FakeResponse(200, payload=payload)


def _rate_limited(retry_after: str | None = None) -> FakeResponse:
    return FakeResponse(429, headers={"Retry-After": retry_after} if retry_after else {})


def test_retry_after_is_honoured_verbatim():
    """The server knows when its bucket refills; guessing shorter only burns
    another request against the same budget."""
    client, session, sleeps = _client([_rate_limited("2"), _ok({"id": "555"})])

    assert client.add_comment("DSE-1", "hello") == "555"
    assert sleeps == [2.0]
    assert len(session.calls) == 2


def test_retry_after_accepts_an_http_date():
    """The header is allowed to carry a date, and a client that only parsed
    integers would fall back to a guess for no reason."""
    when = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=5))
    client, _, sleeps = _client([_rate_limited(when), _ok({"transitions": []})])

    assert client.get_transitions("DSE-1") == []
    assert len(sleeps) == 1
    assert 3.0 <= sleeps[0] <= 6.0  # clock skew inside the test process is sub-second


def test_missing_retry_after_falls_back_to_bounded_jittered_backoff():
    """Without a header the wait grows, but never collapses to ~0: three
    components share the account, so a near-zero retry is a stampede."""
    client, _, sleeps = _client([_rate_limited(), _rate_limited(), _ok({"id": "1"})])

    client.add_comment("DSE-1", "hello")

    assert len(sleeps) == 2
    assert 0.25 <= sleeps[0] <= 0.5  # base 0.5 with equal jitter
    assert 0.5 <= sleeps[1] <= 1.0  # doubled
    assert sleeps[0] > 0


def test_unparseable_retry_after_is_ignored_rather_than_trusted():
    client, _, sleeps = _client([_rate_limited("whenever"), _ok({"id": "1"})])

    client.add_comment("DSE-1", "hello")

    assert len(sleeps) == 1
    assert 0.25 <= sleeps[0] <= 0.5  # the client's own backoff, not the header


def test_gives_up_cleanly_instead_of_retrying_a_sweep_away():
    """A poller sweep has to finish: it is single-threaded on a 60s interval, so
    an unbounded retry loop stalls every other ticket behind this one."""
    client, session, sleeps = _client([_rate_limited()])

    with pytest.raises(JiraRateLimited) as exc:
        client.add_comment("DSE-1", "hello")

    assert exc.value.retries == _RATE_LIMIT_MAX_RETRIES + 1
    assert len(sleeps) == _RATE_LIMIT_MAX_RETRIES
    assert sum(sleeps) <= _RATE_LIMIT_TOTAL_BUDGET
    assert len(session.calls) == _RATE_LIMIT_MAX_RETRIES + 1


def test_a_retry_after_beyond_the_budget_gives_up_without_waiting_at_all():
    """Jira sometimes asks for minutes. Waiting a truncated 30s and retrying
    anyway would just collect another 429 — better to hand the failure back and
    let the next sweep start with a full budget."""
    client, session, sleeps = _client([_rate_limited("300")])

    with pytest.raises(JiraRateLimited) as exc:
        client.transition_issue("DSE-1", "11")

    assert sleeps == []
    assert len(session.calls) == 1
    assert exc.value.retry_after == 300.0


def test_other_http_errors_are_never_swallowed_into_the_retry_path():
    """403/404 are facts about the request, not about timing. Retrying them
    hides the real error behind a delay and multiplies it."""
    for status in (400, 401, 403, 404, 500):
        client, session, sleeps = _client([FakeResponse(status)])
        with pytest.raises(requests.HTTPError):
            client.add_comment("DSE-1", "hello")
        assert sleeps == []
        assert len(session.calls) == 1


def test_the_budget_is_shared_across_a_sweeps_requests_not_reset_per_request():
    """The HIGH finding. `waited` was a local of `_request`, so the "30s" ceiling
    was really 30s times however many requests the caller made — five rate-limited
    calls slept 150s inside a poller interval of 60s. The account is what gets rate
    limited, so the ceiling belongs to the client."""
    clock = FakeClock()
    client, session, sleeps = _client(
        [_rate_limited("8"), _ok({"id": "1"})], session=CyclingSession([_rate_limited("8"), _ok({"id": "1"})]), clock=clock
    )

    # 8s of waiting per request; the fourth would take the client past the budget.
    for _ in range(3):
        client.add_comment("DSE-1", "hello")
    assert sleeps == [8.0, 8.0, 8.0]

    with pytest.raises(JiraRateLimited):
        client.add_comment("DSE-1", "hello")
    assert sleeps == [8.0, 8.0, 8.0]  # it gave up instead of overrunning the sweep
    assert sum(sleeps) <= _RATE_LIMIT_TOTAL_BUDGET


def test_a_whole_sweep_of_rate_limited_requests_stays_inside_the_budget():
    """Whatever the caller does with a rate-limited client, the wall-clock cost of
    429 handling in one window is bounded by one number."""
    clock = FakeClock()
    client, _, sleeps = _client([], session=CyclingSession([_rate_limited("2"), _ok({"id": "1"})]), clock=clock)

    for _ in range(50):
        try:
            client.add_comment("DSE-1", "hello")
        except JiraRateLimited:
            pass

    assert sum(sleeps) <= _RATE_LIMIT_TOTAL_BUDGET


def test_the_budget_refills_so_the_next_sweep_is_not_punished_for_this_one():
    """A window, not a lifetime cap: a client that spent its budget an hour ago
    must not refuse to wait two seconds now."""
    clock = FakeClock()
    client, _, sleeps = _client([_rate_limited("8")], clock=clock)  # Jira never lets up

    with pytest.raises(JiraRateLimited):
        client.add_comment("DSE-1", "hello")
    assert sleeps == [8.0, 8.0, 8.0]  # 24s spent, a 4th wait would not fit

    clock.advance(_RATE_LIMIT_BUDGET_WINDOW + 1)
    with pytest.raises(JiraRateLimited):
        client.add_comment("DSE-1", "hello")
    assert sleeps == [8.0] * 6  # the window slid, so the full budget was available again


def test_the_self_account_id_lookup_happens_once_per_process():
    client, session, _ = _client([_ok({"accountId": "acc_1"})])

    assert [client.self_account_id() for _ in range(5)] == ["acc_1"] * 5
    assert len(session.calls) == 1


def test_a_failed_self_account_id_lookup_is_not_repeated_per_comment():
    """The other half of the HIGH finding: the poller asks for this once per
    comment, and the old cache only remembered successes — so while Jira was rate
    limiting, every comment paid the full backoff for an answer that never changes.
    Caching the failure costs nothing: the filter that actually matters keys on the
    recorded comment id, not on the author."""
    clock = FakeClock()
    client, session, sleeps = _client([_rate_limited("8")], clock=clock)

    for _ in range(5):
        assert client.self_account_id() is None

    assert len(session.calls) <= _RATE_LIMIT_MAX_RETRIES + 1  # one lookup, not five
    assert sum(sleeps) <= _RATE_LIMIT_TOTAL_BUDGET


def test_every_endpoint_shares_the_one_retry_path():
    """The point of a single `_request` is that no call site can forget this."""
    calls = [
        ("add_comment", lambda c: c.add_comment("DSE-1", "b"), {"id": "1"}),
        ("update_comment", lambda c: c.update_comment("DSE-1", "9", "b"), {}),
        ("remove_label", lambda c: c.remove_label("DSE-1", "dse-retry"), {}),
        ("get_transitions", lambda c: c.get_transitions("DSE-1"), {"transitions": []}),
        ("transition_issue", lambda c: c.transition_issue("DSE-1", "11"), {}),
        ("get_comments", lambda c: c.get_comments("DSE-1"), {"comments": []}),
        ("search_updated", lambda c: c.search_updated("DSE", "-5m"), {"issues": [], "isLast": True}),
        ("self_account_id", lambda c: c.self_account_id(), {"accountId": "acc_1"}),
    ]
    for name, call, payload in calls:
        client, session, sleeps = _client([_rate_limited("1"), _ok(payload)])
        call(client)
        assert sleeps == [1.0], f"{name} did not honour Retry-After"
        assert len(session.calls) == 2, f"{name} did not retry the 429"
