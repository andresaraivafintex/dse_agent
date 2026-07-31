"""`update_work_item_status` must project the repo resolved MID-FLIGHT.

THE BUG THESE TESTS EXIST FOR. A work item born on Slack/Jira has no repo: the
admission event carries a channel or a ticket key, not `org/name`. The repo
arrives later, in the human's clarification answer — `ingest_gateway.dispatcher`
extracts the `repo=org/x` marker, the signal fills `input.repo`, the sandbox
CLONES that repo and the run is billed against it. But the only UPDATE of
`work_items` in this service did not list the column, so `work_items.repo`
stayed NULL for the item's whole life. Measured on the live cluster: wi_f404754444
asked for clarification, was answered `repo=andre2654/fintex-wallet`, cloned it,
spent $9.73 — and its `repo` column is still NULL, which is why 89.9% of the
cost rollup is attributed to `(unknown)`.

`base_branch` is resolved by the exact same signal (`branch=` marker) and was
missing from the same statement, so it is covered here too.

WHY BLANK IS TESTED SEPARATELY FROM NULL. `COALESCE(%s, col)` protects the
column against NULL and nothing else. Every later status write goes through this
same statement, so the FIRST caller that renders an unset repo as `""` instead of
None would blank the column on the next transition — turning a fix into a slower
version of the same bug.
"""
from __future__ import annotations

from typing import Any

import psycopg2
import pytest

from conftest import DSN, insert_work_item, new_work_item_id
from dse_orchestrator import local_activities
from dse_orchestrator.local_activities import update_work_item_status

RESOLVED_REPO = "andre2654/fintex-wallet"


@pytest.fixture(autouse=True)
def _require_postgres():
    """Overrides conftest's autouse skip: the normalization tests below fake the
    connection and must RUN where the foundation infra is absent. The tests that
    do need a real row ask for the `postgres` fixture explicitly."""
    yield


@pytest.fixture
def postgres():
    try:
        psycopg2.connect(DSN, connect_timeout=3).close()
    except Exception as exc:  # pragma: no cover - only without the foundation infra
        pytest.skip(f"foundation Postgres unavailable at {DSN}: {exc}")


@pytest.fixture
def slack_item(postgres):
    """A work item as Slack admits one: no repo, no base_branch."""
    work_item_id = new_work_item_id("wi-repoproj")
    insert_work_item(
        work_item_id,
        repo=None,
        base_branch=None,
        source="slack",
        source_ref={"channel": "C0BKA7TMMEY"},
    )
    # No teardown, which is the convention every other suite here follows: the
    # id is unique per run and CI runs against a disposable schema it drops.
    # Deleting needed a second connection as the DSN role, and that role has
    # DELETE revoked on work_items in CI — the fixture passed locally, where the
    # DSN is the owner, and failed the pipeline on teardown for three releases.
    yield work_item_id


def read_repo(work_item_id: str):
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT repo, base_branch FROM work_items WHERE id = %s", (work_item_id,)
            )
            return cur.fetchone()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_a_repo_resolved_after_admission_lands_on_the_row(slack_item):
    """The clarification path end state: the item was born without a repo and the
    human named one. The next status write has to carry it to the column — that
    is the only moment anything can."""
    assert read_repo(slack_item) == (None, None)

    await update_work_item_status(
        {
            "work_item_id": slack_item,
            "status": "implementing",
            "repo": RESOLVED_REPO,
            "base_branch": "main",
        }
    )

    assert read_repo(slack_item) == (RESOLVED_REPO, "main")


@pytest.mark.asyncio
async def test_an_absent_repo_does_not_erase_the_resolved_one(slack_item):
    """Most callers of this Activity have nothing to say about the repo (a CI
    poll, a review round). Their writes must leave the column alone."""
    await update_work_item_status(
        {"work_item_id": slack_item, "status": "implementing",
         "repo": RESOLVED_REPO, "base_branch": "main"}
    )

    await update_work_item_status({"work_item_id": slack_item, "status": "pr_open"})
    await update_work_item_status(
        {"work_item_id": slack_item, "status": "pr_ready", "repo": None, "base_branch": None}
    )

    assert read_repo(slack_item) == (RESOLVED_REPO, "main")


@pytest.mark.asyncio
async def test_a_blank_repo_does_not_erase_the_resolved_one(slack_item):
    """The failure mode COALESCE alone does not cover: a caller that renders "no
    repo" as an empty string writes it straight over the resolved value."""
    await update_work_item_status(
        {"work_item_id": slack_item, "status": "implementing",
         "repo": RESOLVED_REPO, "base_branch": "main"}
    )

    await update_work_item_status(
        {"work_item_id": slack_item, "status": "pr_open", "repo": "", "base_branch": "   "}
    )

    assert read_repo(slack_item) == (RESOLVED_REPO, "main")


# --- the same two guarantees at the driver boundary, without Postgres ---------


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

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        pass

    def close(self):
        pass


def _install(monkeypatch):
    # RETURNING status, state_version, plan_hash, base_sha, head_sha, ci_status
    conn = _FakeConnection(("implementing", 1, None, None, None, None))
    monkeypatch.setattr(local_activities, "_get_connection", lambda: conn)
    return conn


@pytest.mark.asyncio
async def test_the_resolved_repo_is_bound_to_the_statement(monkeypatch):
    """`repo` is not a field of PersistWorkItemStateInput, and the model drops
    unknown keys in silence — reading the resolved value THROUGH it would
    discard it and leave the column NULL with no error anywhere. This is the
    regression guard for that: the value the caller sent reaches the driver."""
    conn = _install(monkeypatch)
    await update_work_item_status(
        {"work_item_id": "wi_1", "status": "implementing",
         "repo": RESOLVED_REPO, "base_branch": "main"}
    )
    _, params = conn.cursor_obj.executed[0]
    assert RESOLVED_REPO in params
    assert "main" in params


@pytest.mark.asyncio
async def test_a_blank_never_reaches_the_driver_as_a_value(monkeypatch):
    """Blank is normalized to NULL BEFORE the parameter, so the COALESCE in the
    statement is enough to preserve the column."""
    conn = _install(monkeypatch)
    await update_work_item_status(
        {"work_item_id": "wi_1", "status": "implementing", "repo": "", "base_branch": "   "}
    )
    _, params = conn.cursor_obj.executed[0]
    assert not [p for p in params if isinstance(p, str) and not p.strip()]
