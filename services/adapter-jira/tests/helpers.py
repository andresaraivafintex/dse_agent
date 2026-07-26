from __future__ import annotations

import hashlib
import hmac

WEBHOOK_SECRET = "jira_webhook_secret_test"


class FakeCursor:
    """Cursor over `FakeConn`. Only the surface the adapter actually uses."""

    def __init__(self, conn: "FakeConn"):
        self._conn = conn
        self._rows: list[tuple] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc) -> None:
        return None

    def execute(self, sql: str, params=None) -> None:
        normalized = " ".join(sql.split())
        self._conn.executed.append((normalized, params))
        self._rows = list(self._conn.responder(normalized, params))

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    """Stand-in for a psycopg2 connection, driven by a `responder(sql, params)`.

    The engine's suites normally run against a real Postgres with the `dse_app`
    role, which does not exist on a developer laptop — every such test errors on
    connect and the actual logic goes unverified. The decision logic under test
    here (which status may be retried, whether a transition is settled or kept)
    is pure branching over a couple of queries, so it does not need a database to
    be exercised; it needs the rows a database would have returned.

    Asserting on `executed` is deliberate as well: "wrote nothing" is half of
    what these features promise, and only the statement log can prove it.
    """

    def __init__(self, responder=None):
        self.responder = responder or (lambda sql, params: [])
        self.executed: list[tuple[str, object]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.autocommit = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True

    def writes(self) -> list[tuple[str, object]]:
        """Every statement that could have changed state — what an append-only
        audit ledger and a queue table care about."""
        return [
            (sql, params)
            for sql, params in self.executed
            if sql.upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ]


def sign(body: bytes) -> str:
    """Jira Cloud X-Hub-Signature signature (HMAC-SHA256, `sha256=<hex>`
    format, the same scheme as GitHub's)."""
    digest = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"
