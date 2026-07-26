"""Guard-rails from the 2nd real run (2026-07-22): lockfile churn and Tester
authoring idempotency.

Run wi_bacdce7 failed ONLY on diff_budget because npm rewrote 16 lines of
package-lock.json while running the tests (with no dependency change) and the
deterministic commit carried the churn into the diff. In the fix cycle, the
Tester authored a NEW test every cycle — the diff only grew and the loop never
converged.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from sandbox_runtime.activities import (
    _restore_lockfile_churn,
    _tester_authored_files_in_history,
)


def _git(ws: str, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ws, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture()
def repo(tmp_path):
    ws = str(tmp_path / "ws")
    os.makedirs(ws)
    _git(ws, "init", "-q", "-b", "main")
    _git(ws, "config", "user.email", "t@dse.local")
    _git(ws, "config", "user.name", "t")
    with open(os.path.join(ws, "package.json"), "w") as fh:
        fh.write('{"name": "app", "dependencies": {}}\n')
    with open(os.path.join(ws, "package-lock.json"), "w") as fh:
        fh.write('{"lockfileVersion": 3}\n')
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", "base")
    return ws


def test_lockfile_churn_without_a_manifest_is_restored(repo):
    with open(os.path.join(repo, "package-lock.json"), "a") as fh:
        fh.write('/* churn do npm */\n')
    restored = _restore_lockfile_churn(repo)
    assert restored == ["package-lock.json"]
    assert _git(repo, "status", "--porcelain").strip() == ""


def test_lockfile_with_a_changed_manifest_stays(repo):
    # the declared dependency changed alongside it: the lockfile+manifest pair is legitimate
    with open(os.path.join(repo, "package.json"), "w") as fh:
        fh.write('{"name": "app", "dependencies": {"left-pad": "^1.0.0"}}\n')
    with open(os.path.join(repo, "package-lock.json"), "a") as fh:
        fh.write('/* new resolution */\n')
    assert _restore_lockfile_churn(repo) == []
    porcelain = _git(repo, "status", "--porcelain")
    assert "package-lock.json" in porcelain and "package.json" in porcelain


def test_new_untracked_lockfile_without_a_manifest_is_removed(repo):
    # e.g. a repo with no yarn.lock; running yarn creates one out of nowhere
    with open(os.path.join(repo, "yarn.lock"), "w") as fh:
        fh.write("# generated\n")
    assert _restore_lockfile_churn(repo) == ["yarn.lock"]
    assert not os.path.exists(os.path.join(repo, "yarn.lock"))


def test_a_normal_modified_file_is_never_touched(repo):
    src = os.path.join(repo, "app.js")
    with open(src, "w") as fh:
        fh.write("// fix\n")
    assert _restore_lockfile_churn(repo) == []
    assert os.path.exists(src)


def test_tester_reuses_tests_from_earlier_commits(repo):
    os.makedirs(os.path.join(repo, "test"))
    with open(os.path.join(repo, "test", "delete.test.js"), "w") as fh:
        fh.write("// authored test\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "tester(wi_x): covers removal by id")
    # a coder commit in between does not count
    with open(os.path.join(repo, "app.js"), "w") as fh:
        fh.write("// fix\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "coder(wi_x): fix remove")
    assert _tester_authored_files_in_history(repo) == ["test/delete.test.js"]


def test_tester_with_no_earlier_commits_authors_normally(repo):
    assert _tester_authored_files_in_history(repo) == []


def test_tester_ignores_an_authored_file_that_vanished(repo):
    os.makedirs(os.path.join(repo, "test"))
    p = os.path.join(repo, "test", "old.test.js")
    with open(p, "w") as fh:
        fh.write("// old\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "tester(wi_x): old")
    _git(repo, "rm", "-q", "test/old.test.js")
    _git(repo, "commit", "-q", "-m", "coder(wi_x): remove an obsolete test")
    assert _tester_authored_files_in_history(repo) == []
