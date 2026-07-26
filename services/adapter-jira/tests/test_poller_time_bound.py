"""The poller's JQL time bound must carry no timezone.

Regression for a silent, weeks-long outage: the poller formatted its UTC cursor
as an absolute JQL literal (`"2026-07-25 19:40"`), and JQL reads absolute
literals in the JIRA ACCOUNT's timezone. On an account set to America/New_York
that asked for issues updated four hours in the FUTURE, so every sweep matched
nothing.

What made it so hard to see: the FIRST sweep has no cursor, so it emits no date
filter at all, succeeds, and ingests a ticket. Everything after it returned zero
while the poller looked perfectly healthy — cursor advancing, no exceptions, no
errors in the log. Nothing failed; the work simply never arrived.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from adapter_jira.poller import _relative_bound

NOW = datetime(2026, 7, 25, 19, 50, tzinfo=timezone.utc)


def test_bound_is_relative_and_never_a_timestamp():
    """The whole point: no date, no time, no offset — nothing to misread."""
    bound = _relative_bound(NOW - timedelta(minutes=30), NOW)
    assert re.fullmatch(r"-\d+m", bound), bound
    # An absolute literal is exactly what caused the outage.
    assert "2026" not in bound
    assert ":" not in bound


def test_first_sweep_has_no_bound_at_all():
    """No cursor yet means "look at everything" — the sweep that accidentally
    worked while the bug was live."""
    assert _relative_bound(None, NOW) is None


def test_window_rounds_up_so_the_cursor_is_always_covered():
    """Losing an update is unrecoverable; re-reading one is free (the event_id
    dedup absorbs it). So a 2m02s gap must ask for 3m, never 2m."""
    assert _relative_bound(NOW - timedelta(seconds=122), NOW) == "-3m"
    assert _relative_bound(NOW - timedelta(minutes=3), NOW) == "-3m"


def test_window_is_never_shorter_than_a_minute():
    """JQL has minute granularity; a sub-minute window would round to `-0m` and
    silently match nothing — the same class of failure all over again."""
    assert _relative_bound(NOW - timedelta(seconds=1), NOW) == "-1m"


def test_long_outage_widens_the_window_instead_of_skipping_work():
    """A poller that was down for hours must re-scan those hours, not resume as
    if nothing happened."""
    assert _relative_bound(NOW - timedelta(hours=3), NOW) == "-180m"
