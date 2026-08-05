"""The operator entry point that proposes an AGENTS.md to a repository.

It is a thin script, but the thin part is the plumbing — the decisions it makes
are not thin at all, and two of them are the difference between a useful draft
and an incident:

  - it must REFUSE when the repository already curates an AGENTS.md. Replacing a
    human's document with a machine draft is the one outcome the whole design
    exists to avoid, and the check has to happen before the model call, not
    after;
  - it must refuse to propose whatever the model happened to return. A response
    that is not a Markdown document does not become one by being committed.

`--dry-run` is tested because it is what an operator should run first, and a
dry run that quietly opens a pull request would be the worst possible bug here.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class _Cfg:
    is_configured = True


class _Client:
    def __init__(self, *, existing_doc=None, tree=None):
        self.existing_doc = existing_doc
        self.tree = tree if tree is not None else ["pom.xml", "src/main/java/A.java"]
        self.proposed = []

    def get_file_text(self, repo, path, ref):
        return self.existing_doc

    def get_tree_paths(self, repo, ref, *, limit=300):
        return self.tree


@pytest.fixture
def cli(monkeypatch):
    """Import the script with its heavy dependencies stubbed. The module reaches
    into the gateway and the GitHub App at import time; none of that is what
    these tests are about."""
    import scripts.propose_agents_md as mod

    client = _Client()
    state = types.SimpleNamespace(client=client, calls=[], proposals=[], content="# AGENTS.md\nUse Maven.\n")

    monkeypatch.setattr(mod, "GitHubConfig", lambda: _Cfg(), raising=True)
    monkeypatch.setattr(mod, "build_github_client", lambda cfg: state.client, raising=True)
    monkeypatch.setattr(
        mod, "mint_virtual_key", lambda h: types.SimpleNamespace(virtual_key="vk"), raising=True
    )

    def _chat(**kw):
        state.calls.append(kw)
        return types.SimpleNamespace(content=state.content)

    def _propose(client, repo, base, *, tree, doc_body, facts):
        state.proposals.append({"repo": repo, "base": base, "body": doc_body})
        return {"number": 5, "html_url": "https://example/pr/5"}

    monkeypatch.setattr(mod, "chat_completion", _chat, raising=True)
    monkeypatch.setattr(mod, "propose_agents_md", _propose, raising=True)
    return mod, state


def _argv(**over):
    args = {"--tenant": "t", "--repo": "acme/api"}
    args.update(over)
    return [x for kv in args.items() for x in kv]


def test_refuses_when_the_repo_already_has_a_document(cli, capsys):
    mod, state = cli
    state.client.existing_doc = "# AGENTS.md written by a human"

    rc = mod.main(_argv())

    assert rc == 0
    assert state.calls == [], "a model was called for a repo that needed nothing"
    assert state.proposals == [], "a human's document was about to be replaced"
    assert "already has an AGENTS.md" in capsys.readouterr().out


def test_dry_run_opens_nothing(cli, capsys):
    mod, state = cli

    rc = mod.main(_argv() + ["--dry-run"])

    assert rc == 0
    assert state.proposals == [], "--dry-run opened a pull request"
    out = capsys.readouterr().out
    assert "Derived facts" in out
    assert "not proposed" in out


def test_refuses_a_response_that_is_not_a_document(cli):
    mod, state = cli
    state.content = "I'd be happy to help you write an AGENTS.md!"

    rc = mod.main(_argv())

    assert rc == 1
    assert state.proposals == []


def test_proposes_the_draft_when_the_repo_has_none(cli, capsys):
    mod, state = cli

    rc = mod.main(_argv())

    assert rc == 0
    assert len(state.proposals) == 1
    assert state.proposals[0]["body"].startswith("# AGENTS.md")
    assert "https://example/pr/5" in capsys.readouterr().out


def test_an_empty_tree_is_an_error_not_an_empty_draft(cli):
    mod, state = cli
    state.client.tree = []

    assert mod.main(_argv()) == 1
    assert state.calls == []


def test_without_a_github_app_it_stops_before_anything_else(cli, monkeypatch):
    mod, state = cli
    monkeypatch.setattr(
        mod, "GitHubConfig", lambda: types.SimpleNamespace(is_configured=False), raising=True
    )

    assert mod.main(_argv()) == 2
    assert state.calls == []


def test_the_model_call_is_bounded(cli):
    """One draft per repository, once — but a pathological repo must not turn a
    two-cent call into an open-ended one."""
    mod, state = cli

    mod.main(_argv())

    assert state.calls[0]["max_tokens"] == mod._MAX_TOKENS
    assert state.calls[0]["temperature"] == 0
