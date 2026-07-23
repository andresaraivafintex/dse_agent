"""Ticks por repo do console (migração 0029 — skill_registry.repo_scope).

Cobre, sem depender de Postgres (fake conn com as 7 colunas da query):
  - semântica de `enabled_for_repo`: NULL=global, "*"=todos, lista=membership,
    []=nenhum;
  - `read_approved_skills(repo=...)` filtra pelos ticks;
  - `repo=None` (legado) não filtra nada — compat com chamadas existentes.
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
    _row("global-null", None),            # nativa do fase1 (sem coluna) — global
    _row("all-star", ["*"]),              # tickada para todos
    _row("only-wallet", ["acme/wallet-svc"]),
    _row("nowhere", []),                  # sem tick — não roda em repo algum
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
