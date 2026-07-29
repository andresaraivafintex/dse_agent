"""The tenant that ran with zero skills for its whole life did so silently:
an empty registry read is a valid return, and nothing on the ledger could be
queried for it. These pin the event and, just as importantly, pin the two cases
that must NOT emit it — a naive `if not materialized` check would cry wolf on
both."""

from __future__ import annotations

import pytest

from sandbox_runtime import sessions
from sandbox_runtime.skill_registry import Skill


def _skill(key: str, body: str = "body") -> Skill:
    return Skill(
        tenant_id="fintex-poc",
        skill_key=key,
        title=f"title {key}",
        body=body,
        category="engineering",
        applies_to=["python"],
    )


# ---------------------------------------------------------------------------
# The Planner's context budget — the difference between "21 skills are served"
# and "21 skills reach the model".
# ---------------------------------------------------------------------------


def _ctx_with_skills(n: int, body_size: int) -> sessions.PlannerContext:
    return sessions.PlannerContext(
        work_item_id="wi_test",
        tenant_id="fintex-poc",
        skills=[_skill(f"skill-{i:02d}", "x" * body_size) for i in range(n)],
        repo_map="## files\nsrc/app.py\nsrc/wallet.py\n",
    )


def test_full_bodies_blow_the_planner_budget():
    """The bug, stated as a test: 21 real skills inline are ~100 KB, and the
    Planner prompt cuts at 8 KB — so the render must be shown to overflow
    before the fix is worth anything."""
    rendered = _ctx_with_skills(21, 4900).render()
    assert len(rendered) > 8000


def test_index_mode_fits_every_skill_inside_the_budget():
    ctx = _ctx_with_skills(21, 4900)
    rendered = ctx.render(skill_body_chars=0)
    assert len(rendered) < 8000
    # every skill is still NAMED — the Planner knows what exists even though it
    # reads none of the bodies (the Coder loads those from files)
    for i in range(21):
        assert f"skill-{i:02d}" in rendered
    # and the sections render() places AFTER the skills survive the truncation
    assert "## Repo map" in rendered[:8000]
    assert "src/wallet.py" in rendered[:8000]


def test_index_mode_points_at_the_file_the_coder_reads():
    rendered = _ctx_with_skills(1, 4900).render(skill_body_chars=0)
    assert ".claude/skills/skill-00/SKILL.md" in rendered
    assert "x" * 100 not in rendered  # the body itself is not inlined


def test_default_render_is_unchanged_for_every_other_caller():
    ctx = _ctx_with_skills(3, 200)
    assert ctx.render() == ctx.render(skill_body_chars=None)
    assert "x" * 200 in ctx.render()


def test_positive_budget_truncates_and_says_so():
    block = _skill("k", "y" * 500).as_context_block(body_chars=100)
    # count on the BODY line only: the header carries "applies to: python",
    # whose own 'y' made the first version of this assertion off by one.
    body_line = block.splitlines()[2]
    assert body_line == "y" * 100
    assert ".claude/skills/k/SKILL.md" in block


# ---------------------------------------------------------------------------
# The audit event itself. This exercises the REAL provision_sandbox rather than
# re-implementing its branch in the test — an earlier draft did the latter and
# would have passed even with the production code untouched. It needs Docker,
# so it runs in CI and skips on a laptop, like every other provision test here.
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("state_dir")
def test_provision_reports_an_empty_registry_read(work_item_id, docker_client, monkeypatch):
    import asyncio

    from dse_contracts import ProvisionSandboxInput

    from sandbox_runtime import activities
    import sandbox_runtime.skill_registry as reg

    rows: list[dict] = []
    monkeypatch.setattr(activities, "audit_emit", lambda **kw: rows.append(kw))
    monkeypatch.setattr(reg, "read_approved_skills", lambda *a, **k: [])

    asyncio.run(activities.provision_sandbox(ProvisionSandboxInput(
        work_item_id=work_item_id, tenant_id="fintex-poc", repo="acme/repo",
    )))

    empties = [r for r in rows if r.get("action") == "skills_resolved_empty"]
    assert len(empties) == 1, "the empty read must be reported exactly once"
    assert empties[0]["details"]["stage"] == "provision"
    assert empties[0]["details"]["repo"] == "acme/repo"
    assert not [r for r in rows if r.get("action") == "skills_materialized"]


@pytest.mark.usefixtures("state_dir")
def test_provision_is_quiet_when_the_repo_already_commits_its_skills(
    work_item_id, docker_client, monkeypatch
):
    """Non-empty read, nothing materialized — the repo ships its own copies.
    That is the documented win case, not a failure, and it is exactly what a
    naive `if not materialized` check would misreport."""
    import asyncio

    from dse_contracts import ProvisionSandboxInput

    from sandbox_runtime import activities
    import sandbox_runtime.skill_files as files
    import sandbox_runtime.skill_registry as reg

    rows: list[dict] = []
    monkeypatch.setattr(activities, "audit_emit", lambda **kw: rows.append(kw))
    monkeypatch.setattr(reg, "read_approved_skills", lambda *a, **k: [_skill("a")])
    monkeypatch.setattr(files, "materialize_skills", lambda *a, **k: [])

    asyncio.run(activities.provision_sandbox(ProvisionSandboxInput(
        work_item_id=work_item_id, tenant_id="fintex-poc", repo="acme/repo",
    )))

    assert not [r for r in rows if r.get("action") == "skills_resolved_empty"]
