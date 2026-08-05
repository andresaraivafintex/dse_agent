"""The read half of the diário de bordo, and the one property that matters most.

The journal competes for the same 16.000-char Planner budget as the skill index,
which already renders at ~10.351 chars against the live registry. So the question
is not whether the journal fits — it is what gets thrown out when it does not.

The answer this pins: the journal goes first, before a single skill is dropped. A
skill is written by a human and approved by a named one; a journal entry is a
machine's note about a past run. Evicting the human's guidance to make room for
the machine's memo inverts the point of the registry, and it would happen
silently, because the skill-eviction loop reports which skills it dropped and
never what it kept them out for.
"""
from __future__ import annotations

import dataclasses

import sandbox_runtime.activities as acts
from sandbox_runtime.run_episodes import RunEpisode, render_episodes_block
from sandbox_runtime.sessions import PlannerContext
from sandbox_runtime.skill_registry import Skill


def _skill(key: str, body_len: int = 500) -> Skill:
    return Skill(
        tenant_id="t", skill_key=key, title=f"Title {key}", body="x" * body_len,
        category="quality", applies_to=["default"],
    )


def _episodes(n: int = 3, size: int = 300) -> list[RunEpisode]:
    return [
        RunEpisode(outcome="failed", digest="d" * size, base_sha="a" * 40, work_item_id=f"WI-{i}")
        for i in range(n)
    ]


def test_journal_is_dropped_before_any_skill_is():
    ctx = PlannerContext(
        work_item_id="WI-1", tenant_id="t",
        skills=[_skill(f"s{i}") for i in range(20)],
        run_episodes=render_episodes_block(_episodes()),
    )
    assert ctx.run_episodes, "precondition: the journal is in the context"

    # The threshold is COMPUTED, not guessed: one char under the full render is
    # the smallest budget that forces an eviction, and it is exactly the case
    # where dropping the cheapest thing must be enough. A hardcoded number here
    # would stop testing anything the day the skill index changes size.
    full = len(ctx.render(skill_body_chars=0))
    without_journal = len(dataclasses.replace(ctx, run_episodes="").render(skill_body_chars=0))
    assert without_journal < full, "precondition: the journal has non-zero cost"

    rendered, tel = acts._fit_planner_context(
        ctx, instruction="fix the login", budget=full - 1
    )

    assert tel["run_episodes_dropped"] is True
    assert tel["skills_dropped"] == [], "a skill was evicted while the journal stayed"
    assert "Recent runs on this repository" not in rendered
    # And dropping it was sufficient — no tail cut on top of it.
    assert tel["context_truncated_chars"] == 0


def test_journal_survives_when_the_budget_is_not_binding():
    ctx = PlannerContext(
        work_item_id="WI-1", tenant_id="t",
        skills=[_skill("s0")],
        run_episodes=render_episodes_block(_episodes(1)),
    )
    rendered, tel = acts._fit_planner_context(ctx, instruction="anything", budget=16_000)

    assert tel["run_episodes_dropped"] is False
    assert "Recent runs on this repository" in rendered


def test_journal_renders_below_the_curated_blocks():
    """Ordering is the visible half of the same ranking: conventions and skills
    are what govern, the journal is what merely happened."""
    ctx = PlannerContext(
        work_item_id="WI-1", tenant_id="t",
        agents_md="# Conventions",
        skills=[_skill("s0")],
        run_episodes=render_episodes_block(_episodes(1)),
        repo_map="repo:app",
    )
    out = ctx.render(skill_body_chars=0)

    assert out.index("AGENTS.md") < out.index("Approved tenant skills")
    assert out.index("Approved tenant skills") < out.index("Recent runs on this repository")
    assert out.index("Recent runs on this repository") < out.index("Repo map")


def test_block_is_bounded_however_many_episodes_come_back():
    from sandbox_runtime.run_episodes import MAX_BLOCK_CHARS

    block = render_episodes_block(_episodes(n=50, size=5_000))
    assert len(block) <= MAX_BLOCK_CHARS + 60
    assert block.endswith("[journal truncated to its planner allowance]")


def test_an_entry_names_the_commit_it_was_recorded_against():
    block = render_episodes_block(
        [RunEpisode(outcome="done", digest="[done] x", base_sha="deadbeef" * 5, work_item_id="WI-9")],
        base_sha="deadbeef" * 5,
    )
    assert "(at deadbeef)" in block
    assert "recorded before the current base" not in block


def test_an_entry_from_an_older_base_says_so():
    """A journal entry is a claim about the repository AT A COMMIT. The model
    reading it deserves to know which — this is the comparison the retrieval
    index never made with its content_sha."""
    block = render_episodes_block(
        [RunEpisode(outcome="done", digest="[done] x", base_sha="0" * 40, work_item_id="WI-9")],
        base_sha="1" * 40,
    )
    assert "recorded before the current base" in block


def test_the_block_frames_entries_as_history_not_instruction():
    """An entry saying 'we could not make the suite pass' must not read as
    permission to skip the suite."""
    block = render_episodes_block(_episodes(1))
    assert "not instructions" in block
    assert "still govern" in block


def test_no_episodes_renders_nothing_at_all():
    assert render_episodes_block([]) == ""
    ctx = PlannerContext(work_item_id="WI-1", tenant_id="t", run_episodes="")
    assert "Recent runs" not in ctx.render(skill_body_chars=0)


def test_a_work_item_without_a_repo_reads_no_journal():
    """Tenant-wide recall would surface another project's history as if it were
    this one's. Not an error — some work items never resolve a repo."""
    from sandbox_runtime.run_episodes import read_recent_episodes

    assert read_recent_episodes("t", repo=None) == []
    assert read_recent_episodes("t", repo="") == []
