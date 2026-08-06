"""The Tester has to be able to fix the test it wrote.

It could not, and the loop could never converge. `_is_dse_authored` recognised
the platform's files by a `-dse` marker in the NAME — but that marker is the
consequence of a rename, not a cause: the FIRST spec the Tester writes has no
marker, because nothing collided with it yet.

So on the next round its own file failed the check, was renamed, and a second
broken copy landed beside the first. Measured on the Angular testbed — four
failing specs accumulated over two rounds:

    report-status-badge.component.spec.ts
    report-status-badge.component-dse.spec.ts
    dashboard-list.component.integration.spec.ts
    dashboard-list.component.integration-dse.spec.ts

Every round the Tester was forbidden from repairing what it had written and
wrote another copy of the same mistake instead.

Git knows the answer exactly: `scoped_git.commit` subject-prefixes every commit
the platform makes, so a file whose history is entirely DSE subjects is ours.
A customer file — even one a DSE commit later touched — keeps a human subject
in its history and stays protected.
"""
from __future__ import annotations

import subprocess

import pytest

from sandbox_runtime.activities import _is_dse_authored


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "wk"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    return r


def _commit(repo, path, subject, body="x"):
    f = repo / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body)
    _git(repo, "add", path)
    _git(repo, "commit", "-q", "-m", subject)


def test_the_first_spec_the_tester_wrote_is_repairable(repo):
    """The regression. This file has NO `-dse` marker — it never collided — and
    the old name-based check called it the customer's, so the next round could
    only stack a copy beside it."""
    _commit(repo, "src/badge.component.spec.ts", "tester(wi_1): add badge spec")
    assert _is_dse_authored("src/badge.component.spec.ts", str(repo)) is True


def test_a_file_the_repository_owns_is_never_overwritten(repo):
    """The property the forbidden-paths rule exists for."""
    _commit(repo, "src/existing.spec.ts", "feat: the customer's own test")
    assert _is_dse_authored("src/existing.spec.ts", str(repo)) is False


def test_a_customer_file_a_dse_commit_touched_is_still_theirs(repo):
    """The subtle one. Authorship is about the whole history, not the last
    commit: the Coder editing a customer test must not transfer ownership of
    it to the platform."""
    _commit(repo, "src/shared.spec.ts", "feat: customer test")
    _commit(repo, "src/shared.spec.ts", "coder(wi_1): adjust the assertion", body="y")
    assert _is_dse_authored("src/shared.spec.ts", str(repo)) is False


def test_a_brand_new_file_with_no_history_is_ours(repo):
    """Written this turn, not yet committed: it is this turn's own output."""
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "fresh.spec.ts").write_text("x")
    assert _is_dse_authored("src/fresh.spec.ts", str(repo)) is True


def test_the_name_marker_still_answers_when_there_is_no_workspace():
    """Unit tests and the Docker dry-run path call this with no repo in hand."""
    assert _is_dse_authored("test/api-dse.test.js") is True
    assert _is_dse_authored("test/api.test.js") is False


def test_git_being_unavailable_falls_back_instead_of_raising(tmp_path):
    """A path that is not a git repository at all must not take the turn down."""
    assert _is_dse_authored("test/api-dse.test.js", str(tmp_path)) is True
