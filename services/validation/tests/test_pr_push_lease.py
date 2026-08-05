"""`push_branch` must survive being called twice.

`--force-with-lease` with no argument leases against the remote-TRACKING ref,
and there is none here: the remote is a bare URL, rebuilt per call so the token
never lands in a git config. Git reads the absent tracking ref as "this branch
should not exist" and rejects every push after the first with `stale info`.

That breaks the two paths the function exists for — re-finalizing after a
review fix, and the activity's own retry after a partial failure — and it
surfaces as a git error that hides whatever actually failed first.

These drive REAL git against REAL repositories, because the defect is in git's
behaviour, not in ours.
"""
from __future__ import annotations

import subprocess

import pytest

from dse_validation.github.pr_finalizer import push_branch


class _LocalExecutor:
    """Runs the argv for real, in `cwd`."""

    def __init__(self, cwd):
        self.cwd = str(cwd)

    def run(self, argv, timeout=None):
        return subprocess.run(argv, cwd=self.cwd, capture_output=True, text=True, timeout=timeout)


class _UrlClient:
    """Stands in for the GitHub client: hands back a bare path, which behaves
    like the tokenised URL — no remote-tracking ref either way."""

    def __init__(self, url):
        self._url = url

    def authenticated_remote_url(self, repo):
        return self._url


@pytest.fixture
def repos(tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True)
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", str(work)], check=True, capture_output=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(work), "config", k, v], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(work), "commit", "-q", "--allow-empty", "-m", "c1"],
        check=True, capture_output=True,
    )
    return _LocalExecutor(work), _UrlClient(str(bare)), work, bare


def _commit(work, msg):
    subprocess.run(
        ["git", "-C", str(work), "commit", "-q", "--allow-empty", "-m", msg],
        check=True, capture_output=True,
    )


def _remote_sha(bare, branch="dse/wi-1"):
    out = subprocess.run(
        ["git", "-C", str(bare), "rev-parse", f"refs/heads/{branch}"],
        capture_output=True, text=True,
    )
    return out.stdout.strip()


def test_the_first_push_creates_the_branch(repos):
    ex, client, _work, bare = repos
    push_branch(ex, client, "owner/repo", "dse/wi-1")
    assert _remote_sha(bare), "the branch was not created"


def test_pushing_again_updates_it_instead_of_being_refused(repos):
    """The re-finalize path: a review fix has to reach the PR. Before the lease
    was made explicit this raised `stale info` and the fix never landed."""
    ex, client, work, bare = repos
    push_branch(ex, client, "owner/repo", "dse/wi-1")
    first = _remote_sha(bare)
    _commit(work, "review fix")
    push_branch(ex, client, "owner/repo", "dse/wi-1")
    second = _remote_sha(bare)
    assert second and second != first, "the second push did not land"


def test_a_commit_we_never_saw_is_still_refused(repos):
    """The lease is not decoration: if someone else moved the branch, the push
    must fail rather than discard their work."""
    ex, client, work, bare = repos
    push_branch(ex, client, "owner/repo", "dse/wi-1")

    # Someone else advances the remote branch behind our back.
    other = work.parent / "other"
    subprocess.run(
        ["git", "clone", "-q", "--branch", "dse/wi-1", str(bare), str(other)],
        check=True, capture_output=True,
    )
    for k, v in (("user.email", "o@o"), ("user.name", "o")):
        subprocess.run(["git", "-C", str(other), "config", k, v], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(other), "commit", "-q", "--allow-empty", "-m", "theirs"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(other), "push", "-q", "origin", "HEAD:refs/heads/dse/wi-1"],
        check=True, capture_output=True,
    )
    theirs = _remote_sha(bare)

    _commit(work, "ours")
    with pytest.raises(RuntimeError, match="push failed"):
        push_branch(ex, client, "owner/repo", "dse/wi-1")
    assert _remote_sha(bare) == theirs, "their commit was clobbered"
