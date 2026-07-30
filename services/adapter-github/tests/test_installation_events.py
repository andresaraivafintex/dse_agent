"""M6 — `installation` / `installation_repositories`: one audit row per
repository, so that marking a repository on the App's installation page leaves a
trace that outlives GitHub's ~7-day delivery log.

Like `test_reconcile.py`, these tests stub the database boundary
(`get_connection`, `resolve_tenant`, `audit_emit`) instead of using a real
Postgres. The branch under test writes nothing but audit rows, and what is being
asserted is WHICH rows it writes, which events it refuses to write them for, and
that a failing write still answers 200. Signature check, routing and payload
reading are 100% real.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import adapter_github.app as app_module
from adapter_github.app import app

from .helpers import sign

client = TestClient(app)

TENANT_ID = "test_tenant_github_adapter"
INSTALLATION_ID = 148035537


@pytest.fixture(autouse=True)
def _cleanup():
    """Overrides the package-wide Postgres cleanup from conftest.py — these
    tests never open a connection, so there is nothing to delete."""
    yield


class _FakeConn:
    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass


def _wire(monkeypatch, *, emit=None) -> list[dict]:
    """Wires the endpoint against fixtures and returns the captured audit rows."""
    audit_rows: list[dict] = []

    monkeypatch.setattr(app_module, "get_connection", lambda: _FakeConn())
    monkeypatch.setattr(
        app_module, "resolve_tenant",
        lambda conn, **kwargs: SimpleNamespace(tenant_id=TENANT_ID, from_binding=True),
    )
    monkeypatch.setattr(app_module, "audit_emit", emit or (lambda **kw: audit_rows.append(kw)))
    return audit_rows


def _post(payload: dict, event_type: str, delivery_id: str) -> dict:
    body = json.dumps(payload).encode()
    resp = client.post(
        "/github/webhook",
        content=body,
        headers={"X-GitHub-Event": event_type, "X-GitHub-Delivery": delivery_id,
                 "X-Hub-Signature-256": sign(body)},
    )
    assert resp.status_code == 200
    return resp.json()


def _repo(full_name: str, *, private: bool = False) -> dict:
    """A repository entry exactly as these two events carry it: five fields, and
    NO `default_branch`."""
    return {
        "id": 111, "node_id": "R_kgDO", "name": full_name.split("/")[-1],
        "full_name": full_name, "private": private,
    }


def _added_payload() -> dict:
    """`installation_repositories` for two repositories marked in one save."""
    return {
        "action": "added",
        "installation": {"id": INSTALLATION_ID, "account": {"login": "andre2654"},
                         "repository_selection": "selected"},
        "repository_selection": "selected",
        "repositories_added": [_repo("andre2654/fintex-wallet"),
                               _repo("andre2654/fintex-demo", private=True)],
        "repositories_removed": [],
        "sender": {"login": "andre2654"},
    }


def test_two_repositories_added_produce_one_audit_row_each(monkeypatch):
    audit_rows = _wire(monkeypatch)

    data = _post(_added_payload(), "installation_repositories", "d-inst-1")

    assert data["path"] == "installation_repositories_audited"
    assert data["audited"] == 2
    assert [r["action"] for r in audit_rows] == ["github_installation_repositories"] * 2
    assert [r["details"]["full_name"] for r in audit_rows] == [
        "andre2654/fintex-wallet", "andre2654/fintex-demo",
    ]
    assert [r["details"]["private"] for r in audit_rows] == [False, True]
    assert audit_rows[0]["tenant_id"] == TENANT_ID
    assert audit_rows[0]["details"]["installation_id"] == INSTALLATION_ID
    assert audit_rows[0]["details"]["action"] == "added"
    assert audit_rows[0]["details"]["repository_selection"] == "selected"


def test_removed_repository_is_audited_too(monkeypatch):
    """Unmarking is the same signal in reverse — a repo the DSE can no longer
    reach — and the payload action is what tells the two apart."""
    audit_rows = _wire(monkeypatch)
    payload = _added_payload()
    payload["action"] = "removed"
    payload["repositories_added"] = []
    payload["repositories_removed"] = [_repo("andre2654/fintex-wallet")]

    data = _post(payload, "installation_repositories", "d-inst-2")

    assert data["audited"] == 1
    assert audit_rows[0]["details"]["full_name"] == "andre2654/fintex-wallet"
    assert audit_rows[0]["details"]["action"] == "removed"
    assert audit_rows[0]["details"]["change"] == "removed"


def test_direction_comes_from_the_array_not_from_the_delivery_action(monkeypatch):
    """Both arrays always ship in the payload, and the handler reads both on
    purpose so a mixed save loses nothing. That makes the delivery's single
    `action` the wrong label per row: taken at face value it would record the
    unmarked repo as "added" — the row reversed, in the ledger built to answer
    exactly which repositories were marked and which were unmarked."""
    audit_rows = _wire(monkeypatch)
    payload = _added_payload()
    payload["action"] = "added"
    payload["repositories_added"] = [_repo("andre2654/fintex-wallet")]
    payload["repositories_removed"] = [_repo("andre2654/fintex-legacy")]

    data = _post(payload, "installation_repositories", "d-inst-6")

    assert data["audited"] == 2
    by_repo = {r["details"]["full_name"]: r["details"] for r in audit_rows}
    assert by_repo["andre2654/fintex-wallet"]["change"] == "added"
    assert by_repo["andre2654/fintex-legacy"]["change"] == "removed"
    # The delivery-level action stays on the row as delivered, unaltered.
    assert {d["action"] for d in by_repo.values()} == {"added"}


def test_payload_without_a_repository_key_does_not_raise(monkeypatch):
    """The trap these two events set: every other handler in this file reads
    `payload["repository"]["full_name"]`, and NEITHER of them has that key. The
    `installation` event also carries its repositories under `repositories`, not
    under the `_added`/`_removed` pair."""
    audit_rows = _wire(monkeypatch)
    payload = {
        "action": "created",
        "installation": {"id": INSTALLATION_ID, "repository_selection": "selected"},
        "repositories": [_repo("andre2654/fintex-wallet")],
        "sender": {"login": "andre2654"},
    }
    assert "repository" not in payload

    data = _post(payload, "installation", "d-inst-3")

    assert data["audited"] == 1
    assert audit_rows[0]["details"]["full_name"] == "andre2654/fintex-wallet"
    # No top-level `repository_selection` on this payload — the installation
    # object carries it.
    assert audit_rows[0]["details"]["repository_selection"] == "selected"


def test_audit_failure_does_not_turn_the_delivery_into_a_500(monkeypatch):
    """`dse_audit.emit` re-raises by construction, and this branch sits where the
    handler used to be a bare `return`. Without the guard, the row that exists to
    record the click would make GitHub retry the very delivery it was watching
    for."""
    def _boom(**kw):
        raise RuntimeError("audit_log is unreachable")

    _wire(monkeypatch, emit=_boom)

    data = _post(_added_payload(), "installation_repositories", "d-inst-4")

    assert data == {"ok": True, "path": "installation_repositories_audited", "audited": 0}


def test_pull_request_still_falls_through_without_auditing(monkeypatch):
    """The allowlist is the point. `pull_request/edited` is fired by the DSE's
    own PATCH on the PR body; auditing it would be the DSE auditing itself, in a
    ledger with no index on `action` that is already mostly CI noise."""
    audit_rows = _wire(monkeypatch)

    data = _post(
        {
            "action": "edited",
            "pull_request": {"number": 12, "merged": False},
            "repository": {"full_name": "andre2654/fintex-wallet"},
            "sender": {"login": "dse-fintex-demo[bot]"},
        },
        "pull_request",
        "d-inst-5",
    )

    assert data == {"ok": True, "path": "ignored_unhandled_event_type"}
    assert audit_rows == []
