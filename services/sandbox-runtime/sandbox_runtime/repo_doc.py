"""Propose an AGENTS.md to a repository that has none — as a pull request.

WHY A PULL REQUEST AND NOT A PROMPT BLOCK. The obvious design is to generate the
document and inject it into the Planner context the way a real AGENTS.md is
injected. Three things kill that:

  1. Nothing verifies prose. A generated claim like "writes go through the
     repository layer" can be paired with a check that `app/repositories` exists,
     and that check will keep passing after the convention is abandoned or
     inverted. A "verified" badge next to an unverifiable sentence manufactures
     confidence rather than establishing it.
  2. `render()` labels AGENTS.md `(trusted)`. Machine-authored prose under that
     header, sitting ABOVE the human-curated skills, would make the model's own
     output outrank the tenant's guidance — and outrank it in the eviction order
     too.
  3. The engine already has a human gate for exactly this shape of artifact, and
     the customer's repository already has one better: code review. A PR is
     reviewed by the people whose conventions it claims to describe.

So the document arrives the way any other proposed change to a repository
arrives. Once it is merged, `_repo_docs_for_planner` picks it up at the base ref
with no further machinery — it is simply a committed AGENTS.md at that point,
indistinguishable from one a human wrote, because a human approved it.

The facts are derived from the file tree deterministically; the model only writes
them up. Everything structural in the document is therefore checkable against the
tree it came from, and the parts a model could invent are prose a reviewer reads.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass

logger = logging.getLogger("sandbox_runtime.repo_doc")

DOC_PATH = "AGENTS.md"
BRANCH = "dse/propose-agents-md"

#: Marker files that identify a build system, in the order they are reported.
_BUILD_MARKERS = (
    ("pom.xml", "Maven (`./mvnw`)"),
    ("build.gradle", "Gradle"),
    ("build.gradle.kts", "Gradle (Kotlin DSL)"),
    ("package.json", "npm/Node"),
    ("pyproject.toml", "Python (pyproject)"),
    ("setup.py", "Python (setup.py)"),
    ("go.mod", "Go modules"),
    ("Cargo.toml", "Cargo"),
    ("Gemfile", "Bundler"),
    ("composer.json", "Composer"),
)

#: Directory names that conventionally hold tests.
_TEST_DIRS = ("tests", "test", "spec", "__tests__", "src/test")


@dataclass(frozen=True)
class RepoFacts:
    """What the tree says, with no model involved. Every field here is a
    statement a reviewer can check by looking at the repository."""

    total_files: int
    build_systems: list[str]
    test_locations: list[str]
    top_dirs: list[tuple[str, int]]
    has_ci: bool
    has_codeowners: bool
    has_contributing: bool

    def as_bullets(self) -> str:
        lines = [f"- {self.total_files} files tracked on the base branch."]
        if self.build_systems:
            lines.append(f"- Build: {', '.join(self.build_systems)}.")
        if self.test_locations:
            lines.append(f"- Tests live under: {', '.join(self.test_locations)}.")
        if self.top_dirs:
            shape = ", ".join(f"`{d}` ({n})" for d, n in self.top_dirs)
            lines.append(f"- Largest top-level directories: {shape}.")
        lines.append(f"- CI workflows: {'yes' if self.has_ci else 'none found'}.")
        lines.append(f"- CODEOWNERS: {'yes' if self.has_codeowners else 'none'}.")
        lines.append(f"- CONTRIBUTING: {'yes' if self.has_contributing else 'none'}.")
        return "\n".join(lines)


def derive_facts(tree: list[str], *, top_n: int = 6) -> RepoFacts:
    """Everything below is a pure function of the tree the Planner already
    fetches. No API call, no model call, no storage."""
    paths = [p for p in tree if p]
    roots: Counter[str] = Counter()
    for p in paths:
        head = p.split("/", 1)[0] if "/" in p else ""
        if head:
            roots[head] += 1

    build = [label for marker, label in _BUILD_MARKERS if marker in paths]
    tests = sorted(
        {d for d in _TEST_DIRS if any(p == d or p.startswith(d + "/") for p in paths)}
    )
    return RepoFacts(
        total_files=len(paths),
        build_systems=build,
        test_locations=tests,
        top_dirs=roots.most_common(top_n),
        has_ci=any(p.startswith(".github/workflows/") for p in paths),
        has_codeowners=any(
            p in ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS") for p in paths
        ),
        has_contributing=any(p.upper().startswith("CONTRIBUTING") for p in paths),
    )


_DOC_PROMPT = """You are writing a first draft of AGENTS.md for a repository that has none.

AGENTS.md tells an autonomous coding agent how to work in THIS repository:
where things live, what the conventions are, how to verify a change.

These facts were derived from the repository's file tree. They are true.

{facts}

Largest directories, with their file counts, so you can infer the layout:
{dirs}

Write the document in Markdown. Rules, all of them binding:
- Ground every statement in the facts above. If the facts do not support a
  claim, do not make it.
- Where a convention would be useful but you cannot verify it from the tree,
  write a TODO line naming what a maintainer should fill in. A TODO a human
  completes is worth more than a guess a human has to catch.
- No praise, no filler, no restating the obvious.
- Under 400 words.
- Start with `# AGENTS.md`.

Return the Markdown only."""


def build_doc_prompt(facts: RepoFacts) -> str:
    dirs = "\n".join(f"  {d}/ — {n} files" for d, n in facts.top_dirs) or "  (flat repository)"
    return _DOC_PROMPT.format(facts=facts.as_bullets(), dirs=dirs)


_PR_BODY = """The DSE plans work in this repository, and it currently has no `AGENTS.md`.
Without one it re-derives the layout and conventions on every task, and gets
them wrong in the ways a newcomer would.

This is a **first draft, not a decision**. Everything structural in it was
derived from this repository's file tree — file counts, build system, where the
tests are — so it is checkable. The prose around those facts was written by a
model and is exactly what wants your eye.

Where a convention could not be established from the tree, the draft leaves a
TODO rather than guessing.

Merge it, edit it, or close it. If you merge it, the DSE's Planner reads it at
the base ref on every task from then on. If you close it, nothing breaks — the
Planner carries on without it.

Facts this draft was derived from:

{facts}
"""


def propose_agents_md(
    client, repo: str, base_branch: str, *, tree: list[str], doc_body: str, facts: RepoFacts
) -> dict:
    """Open the PR. Idempotent by branch name: a re-run updates the draft on the
    same branch rather than opening a second pull request."""
    base_sha = client.get_ref_sha(repo, base_branch)
    if not base_sha:
        raise ValueError(f"{repo}: base branch {base_branch!r} not found")
    client.create_branch(repo, BRANCH, base_sha)
    client.put_file(
        repo,
        DOC_PATH,
        content=doc_body,
        message="Propose an AGENTS.md so the DSE stops re-deriving this repo",
        branch=BRANCH,
    )
    existing = client.get_open_pr_for_branch(repo, BRANCH)
    if existing:
        logger.info("repo_doc: %s already has an open proposal (#%s)", repo, existing.get("number"))
        return existing
    return client.create_pr(
        repo,
        BRANCH,
        base_branch,
        "Propose an AGENTS.md for this repository",
        _PR_BODY.format(facts=facts.as_bullets()),
    )
