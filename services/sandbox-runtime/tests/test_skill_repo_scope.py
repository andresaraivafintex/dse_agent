"""Per-repo ticks from the console (migration 0029 — skill_registry.repo_scope).

Covers, without depending on Postgres (fake conn with the query's 7 columns):
  - `enabled_for_repo` semantics: NULL=global, "*"=all, list=membership,
    []=none;
  - `read_approved_skills(repo=...)` filters by the ticks;
  - `repo=None` (legacy) filters nothing — compat with existing callers.
"""
from __future__ import annotations

from sandbox_runtime.skill_registry import Skill, read_approved_skills


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, _sql, _params):
        pass

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)


def _row(key, repo_scope):
    return ("dev", key, f"title {key}", f"body {key}", "general", ["default"], repo_scope)


_ROWS = [
    _row("global-null", None),            # native to fase1 (no column) — global
    _row("all-star", ["*"]),              # ticked for all
    _row("only-wallet", ["acme/wallet-svc"]),
    _row("nowhere", []),                  # no tick — runs on no repo at all
]


def test_enabled_for_repo_semantics():
    assert Skill("t", "k", "t", "b", "c", repo_scope=None).enabled_for_repo("any/repo")
    assert Skill("t", "k", "t", "b", "c", repo_scope=["*"]).enabled_for_repo("any/repo")
    assert Skill("t", "k", "t", "b", "c", repo_scope=["a/b"]).enabled_for_repo("a/b")
    assert not Skill("t", "k", "t", "b", "c", repo_scope=["a/b"]).enabled_for_repo("x/y")
    assert not Skill("t", "k", "t", "b", "c", repo_scope=[]).enabled_for_repo("a/b")


def test_repo_filter_serves_only_ticked_and_global():
    keys = {s.skill_key for s in read_approved_skills("dev", repo="acme/wallet-svc", conn=_FakeConn(_ROWS))}
    assert keys == {"global-null", "all-star", "only-wallet"}

    other = {s.skill_key for s in read_approved_skills("dev", repo="other/repo", conn=_FakeConn(_ROWS))}
    assert other == {"global-null", "all-star"}


def test_repo_none_keeps_legacy_behavior():
    keys = {s.skill_key for s in read_approved_skills("dev", conn=_FakeConn(_ROWS))}
    assert keys == {"global-null", "all-star", "only-wallet", "nowhere"}
