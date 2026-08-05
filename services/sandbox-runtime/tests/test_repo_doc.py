"""Proposing an AGENTS.md to a repository that has none.

The document arrives as a pull request, not as a prompt block. What is pinned
here is the half that must not depend on a model: every structural statement in
the draft is derived from the file tree, so a reviewer can check it against the
repository rather than against the model's confidence. The prose is the part a
human reads, and the PR is where they read it.

Also pinned: the one outcome this must never produce — overwriting a document a
human already wrote.
"""
from __future__ import annotations

import pytest

from sandbox_runtime.repo_doc import BRANCH, derive_facts, propose_agents_md


def _spring_tree() -> list[str]:
    return (
        ["pom.xml", "mvnw", "README.md", ".github/workflows/ci.yml", "CODEOWNERS"]
        + [f"src/main/java/com/acme/svc/File{i}.java" for i in range(80)]
        + [f"src/test/java/com/acme/svc/File{i}Test.java" for i in range(12)]
        + [f"docs/page{i}.md" for i in range(4)]
    )


def test_facts_are_derived_from_the_tree_alone():
    f = derive_facts(_spring_tree())

    assert f.total_files == len(_spring_tree())
    assert f.build_systems == ["Maven (`./mvnw`)"]
    assert "src/test" in f.test_locations
    assert f.has_ci is True
    assert f.has_codeowners is True
    assert f.has_contributing is False
    assert f.top_dirs[0][0] == "src"


def test_a_flat_repo_does_not_invent_structure():
    f = derive_facts(["README.md", "main.py"])
    assert f.top_dirs == []
    assert f.build_systems == []
    assert f.test_locations == []
    assert "flat repository" in __import__(
        "sandbox_runtime.repo_doc", fromlist=["build_doc_prompt"]
    ).build_doc_prompt(f)


def test_every_bullet_is_checkable_against_the_tree():
    """No bullet may assert something the tree does not show. This is what makes
    the draft reviewable rather than merely plausible."""
    f = derive_facts(["package.json", "src/index.ts", "__tests__/index.test.ts"])
    bullets = f.as_bullets()

    assert "npm/Node" in bullets
    assert "__tests__" in bullets
    assert "CI workflows: none found" in bullets
    assert "CODEOWNERS: none" in bullets


def test_the_prompt_forbids_claims_the_facts_do_not_support():
    from sandbox_runtime.repo_doc import build_doc_prompt

    prompt = build_doc_prompt(derive_facts(_spring_tree()))
    assert "If the facts do not support a" in prompt
    assert "TODO" in prompt


class _Client:
    def __init__(self, *, base_sha="abc123", open_pr=None):
        self.base_sha, self.open_pr = base_sha, open_pr
        self.branches, self.files, self.prs = [], [], []

    def get_ref_sha(self, repo, ref):
        return self.base_sha

    def create_branch(self, repo, branch, from_sha):
        self.branches.append((branch, from_sha))

    def put_file(self, repo, path, *, content, message, branch):
        self.files.append((path, content, branch))

    def get_open_pr_for_branch(self, repo, branch):
        return self.open_pr

    def create_pr(self, repo, head, base, title, body):
        self.prs.append({"head": head, "base": base, "title": title, "body": body})
        return {"number": 1, "html_url": "u"}


def test_the_draft_is_proposed_on_its_own_branch_never_the_base():
    client = _Client()
    facts = derive_facts(_spring_tree())

    propose_agents_md(client, "acme/api", "main", tree=[], doc_body="# AGENTS.md\n", facts=facts)

    assert client.branches == [(BRANCH, "abc123")]
    assert client.files == [("AGENTS.md", "# AGENTS.md\n", BRANCH)]
    assert client.prs[0]["base"] == "main" and client.prs[0]["head"] == BRANCH


def test_a_rerun_updates_the_draft_instead_of_opening_a_second_pr():
    client = _Client(open_pr={"number": 7})
    facts = derive_facts(_spring_tree())

    out = propose_agents_md(client, "acme/api", "main", tree=[], doc_body="# x\n", facts=facts)

    assert out == {"number": 7}
    assert client.prs == [], "a second pull request was opened for the same proposal"


def test_a_missing_base_branch_fails_loudly():
    with pytest.raises(ValueError, match="not found"):
        propose_agents_md(
            _Client(base_sha=None), "acme/api", "nope", tree=[],
            doc_body="# x\n", facts=derive_facts([]),
        )


def test_the_pr_body_carries_the_facts_so_review_can_check_them():
    client = _Client()
    facts = derive_facts(_spring_tree())

    propose_agents_md(client, "acme/api", "main", tree=[], doc_body="# x\n", facts=facts)

    body = client.prs[0]["body"]
    assert "first draft, not a decision" in body
    assert "Maven" in body, "the reviewer cannot check facts the PR does not show them"
