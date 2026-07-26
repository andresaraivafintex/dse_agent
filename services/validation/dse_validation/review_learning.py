"""WSE-E6-T18 — emission of skill-learning episodes from repeated ACCEPTED REVIEW
FEEDBACK.

One of the three "sources at launch" for episodes (§10.17, `skill_episode` table
from migration 0019): recurring clarification (WS-B), CI-repair (WS-E, already
delivered in Phase 3 in `github/l3.py`), and **accepted review feedback** (WS-E,
here).

What it does: when a human review feedback is ACCEPTED (the Coder applied the
requested change and the reviewer accepted it), it records a tenant-scoped
`skill_episode` with `source='review_feedback'`, a DETERMINISTIC `pattern_key`
(groups occurrences of the same feedback pattern) and full provenance (PR,
reviewer, path, comment, diff). `occurrence_n` counts how many times the SAME
pattern (tenant, pattern_key) has been seen — it is the "repeated feedback"
signal that WS-C uses as input to the promotion pipeline (WSC-E4-T2/T3).

BOUNDARY (tested, same as the CI-repair episodes): NO skill is created or
activated here — only the episode. The candidate→eval→approved→canary→active
promotion is 100% WS-C, with human approval (P3: no skill self-promotes).

P1: `pattern_key` is a deterministic text normalization (no LLM).
P3: only feedback ACCEPTED by a human becomes an episode (the producer does not
generate its own learning signal alone — acceptance is the human gate).
P8: every episode emits an audit line.
"""
from __future__ import annotations

import hashlib
import logging

from dse_validation import db

try:
    from dse_audit import emit as audit_emit
except ImportError:  # pragma: no cover
    audit_emit = None

logger = logging.getLogger("dse_validation.review_learning")


def review_pattern_key(comment_body: str, path: str | None = None) -> str:
    """DETERMINISTIC signature of the review-feedback pattern (tenant-scoped in
    the table). Normalizes the text (lower + collapse whitespace) and loosely
    scopes it by the path of the commented file (the same request on the same
    kind of file is the "same pattern"). Short hash, stable across runs.

    This is NOT LLM semantics — it is pure string normalization (P1). Two
    feedbacks with the same normalized text on the same path collide on purpose:
    that collision is exactly the "same repeated pattern" we want to count."""
    normalized = " ".join((comment_body or "").lower().split())
    scope = (path or "").strip().lower()
    raw = f"{scope}|{normalized}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def record_review_feedback_episode(
    *,
    tenant_id: str,
    work_item_id: str,
    pr_number: int | None,
    reviewer: str,
    comment_body: str,
    path: str | None = None,
    diff_hunk: str | None = None,
    accepted: bool = True,
    actor: str = "system:validation",
) -> dict | None:
    """Records the review-feedback episode (if `accepted`). Returns
    `{pattern_key, id, occurrence_n}`, or `None` when `accepted is False`
    (feedback that was not accepted is not a learning signal — P3).

    `reviewer` must be a resolved human principal (via dse_identity) — provenance
    has to be attributable (P8) and acceptance is the human gate (P3)."""
    if not accepted:
        logger.info(
            "review_learning %s: feedback not accepted — no episode recorded", work_item_id
        )
        return None

    pattern_key = review_pattern_key(comment_body, path)
    provenance = {
        "pr_number": pr_number,
        "reviewer": reviewer,
        "path": path,
        "comment": comment_body,
        "diff_hunk": diff_hunk,
        "source": "review_feedback",
    }
    episode = db.record_review_feedback_episode(
        tenant_id=tenant_id,
        work_item_id=work_item_id,
        pattern_key=pattern_key,
        provenance=provenance,
    )

    if audit_emit is not None:
        audit_emit(
            actor=actor,
            action="review_feedback_episode_recorded",
            tenant_id=tenant_id,
            work_item_id=work_item_id,
            details={
                "pattern_key": pattern_key,
                "occurrence_n": episode["occurrence_n"],
                "pr_number": pr_number,
                "reviewer": reviewer,
                "path": path,
                "note": "episode only — no skill created/activated (promotion belongs to WS-C, WSC-E4)",
            },
        )
    return {"pattern_key": pattern_key, **episode}
