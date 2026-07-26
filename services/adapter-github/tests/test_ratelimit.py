"""Rate-limit backoff of the outbound GitHub calls (`adapter_github.ratelimit`,
`RealGithubClient`, the installation-token exchange) and the wait budgets the
endpoints hand it.

Pure transport-level tests: a fake `requests` module stands in for the network,
and the `clock` fixture replaces the `time` module for `ratelimit` (which sleeps),
`auth` (which decides whether a cached token is still fresh) and `app` (which sets
the deadlines), so no Postgres, no network and no real waiting are involved.

The budgets live here rather than in test_outbound.py because that suite needs a
live database and this one does not — and "how long will this endpoint make its
caller wait" is a rate-limit question.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import adapter_github.app as app_module
import adapter_github.auth as auth_module
import adapter_github.backend as backend_module
import adapter_github.ratelimit as ratelimit_module
from adapter_github.app import STATUS_COMMENT_BUDGET_S, StatusCommentRequest
from adapter_github.auth import get_installation_access_token
from adapter_github.backend import RealGithubClient
from adapter_github.ratelimit import (
    GithubRateLimited,
    is_rate_limited,
    request_with_backoff,
    retry_delay_s,
)

from .helpers import Clock


@pytest.fixture(autouse=True)
def _cleanup():
    """Shadows the Postgres cleanup fixture from conftest: nothing here touches
    the database, and the teardown connection would be the only reason these
    tests needed one."""
    yield


@pytest.fixture(autouse=True)
def _empty_token_cache():
    """`auth._TOKEN_CACHE` is module state that outlives a test. Left dirty, one
    test's cached token silently answers another test's exchange."""
    auth_module._TOKEN_CACHE.clear()
    yield
    auth_module._TOKEN_CACHE.clear()


@pytest.fixture
def clock(monkeypatch) -> Clock:
    """Replaces the `time` module for the code that sleeps AND for the code that
    computes the deadlines.

    Both, because a deadline built from the real `time.time()` and compared against
    a fake clock is two unrelated numbers: the comparison then says nothing about
    the budget and the assertion holds no matter what the code does. And `sleep`
    has to advance the clock (see `helpers.Clock`) or the budget never depletes.
    """
    fake = Clock()
    monkeypatch.setattr(ratelimit_module, "time", fake)
    monkeypatch.setattr(auth_module, "time", fake)
    monkeypatch.setattr(app_module, "time", fake)
    return fake


class FakeResponse:
    def __init__(self, status_code: int, *, headers: dict | None = None, payload=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeRequests:
    """Stands in for the `requests` module inside `adapter_github.backend`:
    returns a scripted response per call and records what was sent. A script down
    to its last entry keeps replaying it, so a test can say "throttled from here
    on" without counting attempts."""

    def __init__(self, responses: list[FakeResponse]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]

    def post(self, url, **kwargs):
        """`auth` posts through `requests.post`; same script, same recording."""
        return self.request("POST", url, **kwargs)


def _primary_limit(reset_at: float) -> FakeResponse:
    return FakeResponse(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(reset_at))})


def _secondary_limit(retry_after: int) -> FakeResponse:
    return FakeResponse(429, headers={"Retry-After": str(retry_after)})


class _MemoryCommentStore:
    """`CommentStateStore` in memory. Stands in for the Postgres store only so the
    status-comment endpoint can be called without a database; what these tests
    assert is the waiting, not the persistence."""

    def __init__(self) -> None:
        self.refs: dict[tuple[str, str], str] = {}

    def get_ref(self, work_item_id: str, surface: str) -> str | None:
        return self.refs.get((work_item_id, surface))

    def save_ref(self, work_item_id: str, surface: str, comment_ref: str) -> None:
        self.refs[(work_item_id, surface)] = comment_ref


# ---------------------------------------------------------------------------
# Classification and delay
# ---------------------------------------------------------------------------
def test_permission_403_is_not_a_rate_limit():
    # A 403 with neither marker is the App missing a permission: retrying it can
    # never succeed and would burn the reconciler's budget.
    assert not is_rate_limited(FakeResponse(403, payload={"message": "Resource not accessible"}))
    assert not is_rate_limited(FakeResponse(404))
    assert not is_rate_limited(FakeResponse(500))
    assert not is_rate_limited(FakeResponse(201))


def test_both_throttle_shapes_are_recognised():
    assert is_rate_limited(_primary_limit(reset_at=100))
    assert is_rate_limited(_secondary_limit(retry_after=3))
    assert is_rate_limited(FakeResponse(403, headers={"retry-after": "3"}))  # lowercase header


def test_retry_after_wins_over_backoff():
    assert retry_delay_s(_secondary_limit(retry_after=7), attempt=0, now=0.0) == 7.0


def test_primary_limit_waits_until_reset_plus_a_second():
    # The quota refills AT `reset`; waking up exactly then costs another 403.
    assert retry_delay_s(_primary_limit(reset_at=1_000_020), attempt=0, now=1_000_000.0) == 20.0 + 1.0


def test_a_server_hint_is_never_truncated_to_something_we_would_rather_wait():
    """HIGH: a primary limit 40 minutes out means 40 minutes. `min(2400, 30)`
    returned 30s, and re-requesting after 30s hits a quota that provably has not
    refilled — and on the secondary/abuse limit GitHub documents that requesting
    before `Retry-After` elapses can EXTEND the block."""
    assert retry_delay_s(_primary_limit(reset_at=1_002_400), attempt=0, now=1_000_000.0) == 2401.0
    assert retry_delay_s(_secondary_limit(retry_after=600), attempt=0, now=0.0) == 600.0


def test_reset_in_the_past_does_not_produce_a_negative_sleep():
    assert retry_delay_s(_primary_limit(reset_at=10), attempt=0, now=1_000.0) == 1.0


def test_backoff_grows_and_is_jittered_without_a_server_hint():
    unhinted = FakeResponse(429, headers={"Retry-After": "not-a-number"})
    first = [retry_delay_s(unhinted, attempt=0, now=0.0) for _ in range(20)]
    later = [retry_delay_s(unhinted, attempt=3, now=0.0) for _ in range(20)]
    assert all(0.5 <= d <= 1.0 for d in first)
    assert all(4.0 <= d <= 8.0 for d in later)
    assert len(set(first)) > 1  # jitter, not a fixed schedule


# ---------------------------------------------------------------------------
# The retry loop
# ---------------------------------------------------------------------------
def test_a_throttled_call_succeeds_after_backing_off(clock):
    responses = [_secondary_limit(2), _secondary_limit(3), FakeResponse(200, payload={"id": 7})]
    resp = request_with_backoff(
        lambda: responses.pop(0), what="create_comment", deadline=clock.time() + 60.0
    )
    assert resp.status_code == 200
    assert clock.slept == [2.0, 3.0]


def test_non_throttle_response_is_returned_untouched_and_never_slept_on(clock):
    resp = request_with_backoff(
        lambda: FakeResponse(422), what="create_comment", deadline=clock.time() + 60.0
    )
    assert resp.status_code == 422
    assert clock.slept == []


def test_a_hint_bigger_than_the_budget_gives_up_without_requesting_again(clock):
    """One request, then out. Sleeping the part we can afford and asking anyway is
    exactly the early poke GitHub says can extend an abuse block."""
    sent: list[int] = []
    reset_at = clock.time() + 2400  # 40 minutes out, a real primary-limit answer

    def _send():
        sent.append(1)
        return _primary_limit(reset_at=reset_at)

    with pytest.raises(GithubRateLimited) as excinfo:
        request_with_backoff(_send, what="create_comment", deadline=clock.time() + 60.0)

    assert clock.slept == []
    assert len(sent) == 1
    assert excinfo.value.response.status_code == 403


def test_the_budget_left_is_reported_when_it_runs_out(clock):
    """The reconciler logs this exception; without the remaining budget in the
    message there is no telling "we ran out of time" from "GitHub is down" in the
    CronJob output."""
    with pytest.raises(GithubRateLimited) as excinfo:
        request_with_backoff(
            lambda: _secondary_limit(25), what="create_comment", deadline=clock.time() + 60.0
        )
    assert clock.slept == [25.0, 25.0]  # a third 25s wait would exceed the budget
    assert "10.0s of budget left" in str(excinfo.value)


def test_attempts_are_bounded_even_with_a_tiny_hint(clock):
    with pytest.raises(GithubRateLimited):
        request_with_backoff(
            lambda: _secondary_limit(1), what="get_issue", deadline=clock.time() + 60.0
        )
    assert len(clock.slept) == 3  # MAX_ATTEMPTS calls means MAX_ATTEMPTS - 1 sleeps


# ---------------------------------------------------------------------------
# One budget per caller, not per request
# ---------------------------------------------------------------------------
def test_one_client_spends_one_budget_across_every_request(clock, monkeypatch):
    """BLOCKER: `/internal/reconcile` walks every pending thread in ONE request and
    `list_issue_comments` pages up to five times per thread. With a budget that
    restarted per request, the sweep was entitled to dozens of independent budgets
    — tens of minutes of sleeping inside a request the CronJob abandons after 120s.
    One client, one deadline."""
    fake = FakeRequests([_secondary_limit(25)])
    monkeypatch.setattr(backend_module, "requests", fake)

    client = RealGithubClient("tok", deadline=clock.time() + 60.0)

    for _ in range(5):  # five threads of one sweep, all throttled
        with pytest.raises(GithubRateLimited):
            client.get_issue("acme/widgets", 1)

    assert clock.slept == [25.0, 25.0]  # the whole sweep, not per thread
    assert sum(clock.slept) <= 60.0


class _FakeConfig:
    """`adapter_github.config` for `build_real_github_client` — the App
    credentials are read through it and none of them reach a network here."""

    @staticmethod
    def get_app_id() -> str:
        return "1"

    @staticmethod
    def get_app_private_key() -> str:
        return "pem"

    @staticmethod
    def get_installation_id() -> str:
        return "2"


def test_the_token_exchange_and_the_client_share_the_callers_budget(clock, monkeypatch):
    """MEDIUM/HIGH: the status-comment endpoint used to pay a full budget TWICE —
    once for the uncached installation-token exchange, once for the comment
    itself. One deadline threaded through both is what makes the endpoint's
    promise to its 8s caller arithmetically possible."""
    monkeypatch.setattr(auth_module, "generate_app_jwt", lambda *a, **k: "jwt")
    monkeypatch.setitem(backend_module.__dict__, "config", _FakeConfig())

    exchange = FakeRequests([_secondary_limit(8), FakeResponse(201, payload={"token": "ghs_x"})])
    monkeypatch.setattr(auth_module, "requests", exchange)
    comments = FakeRequests([_secondary_limit(8)])
    monkeypatch.setattr(backend_module, "requests", comments)

    client = backend_module.build_real_github_client(deadline=clock.time() + 10.0)
    with pytest.raises(GithubRateLimited):
        client.create_comment("acme/widgets", 12, "body")

    # 8s went to the token exchange, so the comment's own 8s hint no longer fits
    # in what is left of the caller's 10s. Two independent budgets would have
    # slept twice and answered the orchestrator after 16s — long past the 8s at
    # which it walks away.
    assert clock.slept == [8.0]
    assert len(comments.calls) == 1


# ---------------------------------------------------------------------------
# The real client
# ---------------------------------------------------------------------------
def test_create_comment_retries_the_same_request_and_posts_once(clock, monkeypatch):
    fake = FakeRequests([_primary_limit(reset_at=0), FakeResponse(201, payload={"id": 4242})])
    monkeypatch.setattr(backend_module, "requests", fake)

    client = RealGithubClient("tok", deadline=clock.time() + 60.0)
    assert client.create_comment("acme/widgets", 12, "body") == 4242
    assert [c["method"] for c in fake.calls] == ["POST", "POST"]
    assert {c["json"]["body"] for c in fake.calls} == {"body"}  # verbatim repeat, no mutation


def test_update_comment_surfaces_a_permission_403_instead_of_retrying(clock, monkeypatch):
    fake = FakeRequests([FakeResponse(403, payload={"message": "Resource not accessible"})])
    monkeypatch.setattr(backend_module, "requests", fake)

    client = RealGithubClient("tok", deadline=clock.time() + 60.0)
    with pytest.raises(RuntimeError):  # raise_for_status, not GithubRateLimited
        client.update_comment("acme/widgets", 99, "body")
    assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# Installation token: backoff and caching
# ---------------------------------------------------------------------------
class FakeSession:
    """Scripted `post` that counts exchanges — the count IS what the caching tests
    are about."""

    def __init__(self, responses: list[FakeResponse]):
        self._responses = list(responses)
        self.calls = 0

    def post(self, url, **kwargs):
        self.calls += 1
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]


def _token_response(expires_at: str | None, token: str = "ghs_real") -> FakeResponse:
    payload: dict = {"token": token}
    if expires_at is not None:
        payload["expires_at"] = expires_at
    return FakeResponse(201, payload=payload)


def _utc_stamp(epoch: float) -> str:
    """`expires_at` in the shape GitHub sends it."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_installation_token_exchange_backs_off(clock, monkeypatch):
    monkeypatch.setattr(auth_module, "generate_app_jwt", lambda *a, **k: "jwt")

    session = FakeSession([_secondary_limit(1), _token_response(_utc_stamp(clock.time() + 3600))])
    token = get_installation_access_token(
        app_id="1", private_key_pem="pem", installation_id="2",
        deadline=clock.time() + 60.0, session=session,
    )
    assert token == "ghs_real"
    assert session.calls == 2
    assert clock.slept == [1.0]


def test_the_installation_token_is_reused_instead_of_re_minted_per_request(clock, monkeypatch):
    """MEDIUM: `build_real_github_client` runs on every `/internal/status-comment`
    request. An uncached exchange means every status update pays an extra
    round-trip that can itself be throttled, spending the caller's budget before
    the comment it was made for."""
    monkeypatch.setattr(auth_module, "generate_app_jwt", lambda *a, **k: "jwt")

    session = FakeSession([_token_response(_utc_stamp(clock.time() + 3600), token="ghs_first")])
    kwargs = dict(app_id="1", private_key_pem="pem", installation_id="2",
                  deadline=clock.time() + 60.0, session=session)

    assert get_installation_access_token(**kwargs) == "ghs_first"
    assert get_installation_access_token(**kwargs) == "ghs_first"
    assert get_installation_access_token(**kwargs) == "ghs_first"
    assert session.calls == 1


def test_a_token_about_to_expire_is_re_minted_before_it_dies_mid_sweep(clock, monkeypatch):
    """A token still valid when handed out but dead a minute later fails every
    remaining call of the sweep with a 401 no retry can fix."""
    monkeypatch.setattr(auth_module, "generate_app_jwt", lambda *a, **k: "jwt")

    about_to_expire = clock.time() + auth_module._TOKEN_REFRESH_MARGIN_S + 30.0
    session = FakeSession([
        _token_response(_utc_stamp(about_to_expire), token="ghs_first"),
        _token_response(_utc_stamp(clock.time() + 3600), token="ghs_second"),
    ])
    kwargs = dict(app_id="1", private_key_pem="pem", installation_id="2",
                  deadline=clock.time() + 60.0, session=session)

    assert get_installation_access_token(**kwargs) == "ghs_first"
    clock.t += 60.0  # now inside the refresh margin
    assert get_installation_access_token(**kwargs) == "ghs_second"
    assert session.calls == 2


@pytest.mark.parametrize("expires_at", [None, "not-a-timestamp", "2099-01-01T00:00:00"])
def test_a_token_with_no_usable_expiry_is_used_once_and_never_cached(clock, monkeypatch, expires_at):
    """Caching a token whose lifetime we had to guess is a 401 waiting to happen.
    A NAIVE timestamp counts as unusable: read as local time it can land the
    expiry hours from the truth, in either direction."""
    monkeypatch.setattr(auth_module, "generate_app_jwt", lambda *a, **k: "jwt")

    session = FakeSession([_token_response(expires_at)])
    kwargs = dict(app_id="1", private_key_pem="pem", installation_id="2",
                  deadline=clock.time() + 60.0, session=session)

    assert get_installation_access_token(**kwargs) == "ghs_real"
    assert get_installation_access_token(**kwargs) == "ghs_real"
    assert session.calls == 2


def test_tokens_of_two_installations_are_never_confused(clock, monkeypatch):
    monkeypatch.setattr(auth_module, "generate_app_jwt", lambda *a, **k: "jwt")

    expiry = _utc_stamp(clock.time() + 3600)
    first = FakeSession([_token_response(expiry, token="ghs_inst_1")])
    second = FakeSession([_token_response(expiry, token="ghs_inst_2")])
    common = dict(app_id="1", private_key_pem="pem", deadline=clock.time() + 60.0)

    assert get_installation_access_token(installation_id="1", session=first, **common) == "ghs_inst_1"
    assert get_installation_access_token(installation_id="2", session=second, **common) == "ghs_inst_2"
    assert get_installation_access_token(installation_id="1", session=first, **common) == "ghs_inst_1"
    assert (first.calls, second.calls) == (1, 1)


# ---------------------------------------------------------------------------
# The budget the status-comment endpoint chooses
# ---------------------------------------------------------------------------
def _status_comment_request() -> StatusCommentRequest:
    return StatusCommentRequest(
        work_item_id="wi_budget", repo="acme/widgets", issue_number=42, body="queued",
        actor="system:orchestrator",
    )


def _wire_status_comment(monkeypatch, fake: FakeRequests) -> None:
    """Runs the real endpoint function with the real client and the real backoff —
    only the Postgres store, the audit write and the App-token exchange are
    stubbed."""
    monkeypatch.setattr(app_module, "PgCommentStateStore", _MemoryCommentStore)
    monkeypatch.setattr(app_module, "audit_emit", lambda **kw: None)
    monkeypatch.setattr(backend_module, "requests", fake)
    monkeypatch.setattr(
        app_module, "build_real_github_client",
        lambda *, deadline: RealGithubClient("tok", deadline=deadline),
    )


def test_status_comment_gives_up_before_the_orchestrator_stops_waiting(clock, monkeypatch):
    """HIGH: the orchestrator posts status comments best-effort with an 8s timeout.
    A retry that outlives it is worse than none: the orchestrator walks away,
    `MutableCommentWriter` never saves the ref, and the NEXT transition finds no
    ref and posts a SECOND comment — breaking the one invariant the mutable
    comment exists to keep. So a 5s hint must not be slept on."""
    fake = FakeRequests([_secondary_limit(5)])
    _wire_status_comment(monkeypatch, fake)

    with pytest.raises(GithubRateLimited):
        app_module.upsert_status_comment(_status_comment_request())

    assert clock.slept == []
    assert len(fake.calls) == 1  # nothing was posted, so nothing to duplicate
    assert STATUS_COMMENT_BUDGET_S < 8.0


def test_status_comment_still_absorbs_a_short_throttle(clock, monkeypatch):
    """The other half of the bound: the budget is small, not zero. A one-second
    secondary limit must still be waited out, or every mildly throttled transition
    loses its status comment."""
    fake = FakeRequests([_secondary_limit(1), FakeResponse(201, payload={"id": 777})])
    _wire_status_comment(monkeypatch, fake)

    result = app_module.upsert_status_comment(_status_comment_request())

    assert result["ok"] is True
    assert clock.slept == [1.0]
    assert [c["method"] for c in fake.calls] == ["POST", "POST"]
