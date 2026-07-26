"""WSE-E6-T18 — skill-learning episodes from accepted review feedback.

Real Postgres (skill_episode from migration 0019 + audit). Proves:
  - repeated accepted feedback (same pattern) => episodes with increasing
    occurrence_n, tenant-scoped;
  - full provenance (PR/reviewer/diff);
  - BOUNDARY: NO skill is created/activated (skill_registry untouched);
  - feedback that was NOT accepted does not become an episode (P3);
  - per-tenant isolation of occurrence_n.
"""
from __future__ import annotations

import uuid

import pytest

from dse_validation import db
from dse_validation.review_learning import record_review_feedback_episode, review_pattern_key


@pytest.fixture
def tenant_id():
    return f"acme-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def work_item_id():
    return f"wi_{uuid.uuid4().hex[:8]}"


def test_repeated_accepted_feedback_increments_occurrence(tenant_id, work_item_id):
    body = "Use a parameterised query instead of an f-string in the SQL."
    path = "app/dao.py"

    first = record_review_feedback_episode(
        tenant_id=tenant_id, work_item_id=work_item_id, pr_number=101,
        reviewer="usr_alice", comment_body=body, path=path,
        diff_hunk="- cur.execute(f\"...{x}\")\n+ cur.execute(\"...%s\", (x,))",
        accepted=True,
    )
    assert first is not None
    assert first["occurrence_n"] == 1

    # SAME pattern, different PR/work item — occurrence_n goes up (repeated feedback)
    wi2 = f"wi_{uuid.uuid4().hex[:8]}"
    second = record_review_feedback_episode(
        tenant_id=tenant_id, work_item_id=wi2, pr_number=102,
        reviewer="usr_bob", comment_body=body.upper(),  # normalization: same pattern
        path=path, accepted=True,
    )
    assert second["occurrence_n"] == 2
    assert second["pattern_key"] == first["pattern_key"]

    episodes = db.list_skill_episodes(tenant_id, source="review_feedback",
                                      pattern_key=first["pattern_key"])
    assert len(episodes) == 2
    assert episodes[0]["provenance"]["reviewer"] == "usr_alice"
    assert episodes[0]["provenance"]["pr_number"] == 101
    assert "diff_hunk" in episodes[0]["provenance"]


def test_boundary_no_skill_created_or_activated(tenant_id, work_item_id):
    """The BOUNDARY under test: recording an episode does NOT create/activate any skill."""
    before = db.count_skill_registry(tenant_id)

    ep = record_review_feedback_episode(
        tenant_id=tenant_id, work_item_id=work_item_id, pr_number=200,
        reviewer="usr_alice", comment_body="Add None handling here.",
        path="app/svc.py", accepted=True,
    )
    assert ep is not None

    after = db.count_skill_registry(tenant_id)
    assert after == before, "recording an episode must NOT create/activate a skill (promotion belongs to WS-C)"

    # the episode exists (the governable input)
    assert len(db.list_skill_episodes(tenant_id, source="review_feedback")) == 1
    # audit (P8)
    assert _audit_count(work_item_id, "review_feedback_episode_recorded") == 1


def test_not_accepted_feedback_records_nothing(tenant_id, work_item_id):
    result = record_review_feedback_episode(
        tenant_id=tenant_id, work_item_id=work_item_id, pr_number=300,
        reviewer="usr_alice", comment_body="maybe rethink this", path="x.py",
        accepted=False,
    )
    assert result is None
    assert db.list_skill_episodes(tenant_id, source="review_feedback") == []
    assert _audit_count(work_item_id, "review_feedback_episode_recorded") == 0


def test_pattern_key_deterministic_and_path_scoped():
    a = review_pattern_key("Use  parametrized   query", "app/dao.py")
    b = review_pattern_key("use parametrized query", "app/dao.py")
    assert a == b  # normalization (lower + collapse whitespace)
    c = review_pattern_key("use parametrized query", "app/other.py")
    assert c != a  # path scopes the pattern


def test_tenant_scoped_occurrence(work_item_id):
    body = "Extract this magic constant."
    t1 = f"acme-{uuid.uuid4().hex[:8]}"
    t2 = f"globex-{uuid.uuid4().hex[:8]}"
    e1 = record_review_feedback_episode(
        tenant_id=t1, work_item_id=work_item_id, pr_number=1, reviewer="usr_a",
        comment_body=body, path="c.py", accepted=True,
    )
    e2 = record_review_feedback_episode(
        tenant_id=t2, work_item_id=work_item_id, pr_number=1, reviewer="usr_a",
        comment_body=body, path="c.py", accepted=True,
    )
    # same pattern_key (same text/path) but occurrence_n restarts per tenant
    assert e1["pattern_key"] == e2["pattern_key"]
    assert e1["occurrence_n"] == 1
    assert e2["occurrence_n"] == 1


def _audit_count(work_item_id: str, action: str) -> int:
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM audit_log WHERE work_item_id = %s AND action = %s",
                (work_item_id, action),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()
