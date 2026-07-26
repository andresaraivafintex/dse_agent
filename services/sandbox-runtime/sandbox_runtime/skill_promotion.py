"""Skill promotion pipeline (WSC-E4-T2 + T3) — Phase 4.

Two halves, both DETERMINISTIC (P1 — no flow decision made by an LLM):

  1. **Episode capture + candidate materialization (T2).** The three "sources at
     launch" (§10.17) record episodes in `skill_episode`: `clarification`
     (WS-B), `ci_repair` and `review_feedback` (WS-E). When a `pattern_key`
     accumulates `SUM(occurrence_n) >= threshold` (config, not an LLM),
     `materialize_candidates` creates a skill in `skill_registry` with
     `status='candidate'`, an incremented `version` and full provenance. A
     candidate is NOT served to the Planner (only {approved, active} are) and
     does NOT self-promote — it needs an eval + human approval (P3).

  2. **Governed promotion pipeline (T3).** Explicit state machine
     candidate → approved → canary → active (+ rollback active/canary →
     rolled_back). NON-NEGOTIABLE invariants, by construction (there is no code
     path that violates them — they raise BEFORE any write):
       - P1/P3: a transition into {approved, active} requires a resolved,
         non-empty human `approver` (`ApproverRequired`); a `system:*` never
         approves (no skill self-promotes).
       - candidate → approved requires a PASSING eval (negative_regressions=0)
         recorded in `skill_eval` — the promotion is blocked by construction if
         the replay regressed on a negative case (`EvalGateNotPassed`).
       - an illegal transition in the state machine raises `IllegalTransition`.
     Rollback is a POINTER change within one transaction (failure mode 13): the
     active version becomes `rolled_back` and the previously served version goes
     back to `active` — in seconds, reprocessing nothing. The partial unique
     index `uq_skill_registry_one_served` structurally guarantees there are never
     two served versions of the same skill.

Every consequential transition writes to `dse_audit.emit` with the approver's
identity (P8). Real Postgres, clean failure when unavailable (P6) — it never
silently degrades to "no skills".
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import psycopg2
from psycopg2.extras import Json

from dse_audit import emit as audit_emit

_DSN = os.environ.get(
    "DSE_SANDBOX_DATABASE_URL",
    os.environ.get("DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"),
)

# Valid episode sources (matches the CHECK in migration 0019).
EPISODE_SOURCES = ("clarification", "ci_repair", "review_feedback")

# Materialization threshold — CONFIG, not an LLM (P1). How many occurrences of
# the same pattern_key before it becomes a candidate.
DEFAULT_CANDIDATE_THRESHOLD = int(os.environ.get("DSE_SKILL_CANDIDATE_THRESHOLD", "3"))

# What the PRODUCTION Planner sees. candidate/canary/draft/rolled_back/retired
# are NEVER served. canary = shadow (no canary-subset selection at this phase —
# documented in the README).
SERVED_STATUSES = ("approved", "active")

# Governed state machine. Map from_status -> {allowed to_status}.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "candidate": {"approved"},
    "approved": {"canary", "active", "retired"},
    "canary": {"active", "rolled_back"},
    "active": {"rolled_back"},
}

# Transitions that require a named HUMAN approver (P1/P3 — non-negotiable).
APPROVER_REQUIRED_TO = {"approved", "active"}


class SkillPromotionUnavailable(Exception):
    """Postgres unavailable in the pipeline — clean failure (P6)."""


class ApproverRequired(Exception):
    """An attempt to promote to approved/active without a valid human approver.
    Raised BEFORE any write — promotion without a human is impossible by
    construction (P1/P3)."""


class EvalGateNotPassed(Exception):
    """candidate → approved with no recorded passing eval (negative_regressions=0).
    Blocked by construction (P3 — a candidate does not approve itself)."""


class IllegalTransition(Exception):
    """A transition outside the governed state machine."""


class SkillNotFound(Exception):
    """(tenant, skill_key, version) does not exist."""


def _connect(dsn: str | None = None):
    try:
        return psycopg2.connect(dsn or _DSN)
    except Exception as exc:  # noqa: BLE001
        raise SkillPromotionUnavailable(f"skill_promotion: Postgres unavailable: {exc}") from exc


def _is_human_principal(approver: str | None) -> bool:
    """The approver must be a resolved human principal — non-empty and not a
    system actor (P3: no skill self-promotes through a `system:*`)."""
    if not approver or not approver.strip():
        return False
    return not approver.strip().startswith("system:")


# ===========================================================================
# WSC-E4-T2 — episode capture + candidate materialization
# ===========================================================================
def record_episode(
    tenant_id: str,
    source: str,
    pattern_key: str,
    *,
    work_item_id: str | None = None,
    occurrence_n: int = 1,
    provenance: dict[str, Any] | None = None,
    conn=None,
) -> int:
    """Record a learning episode (one occurrence of a `pattern_key`).
    Deterministic and append-only — it creates/activates NO skill here, it is
    only the governable raw input. Returns the episode id."""
    if source not in EPISODE_SOURCES:
        raise ValueError(f"invalid source: {source!r} (expected {EPISODE_SOURCES})")
    if not tenant_id or not tenant_id.strip():
        raise ValueError("tenant_id is mandatory")
    if not pattern_key or not pattern_key.strip():
        raise ValueError("pattern_key is mandatory")

    owns = conn is None
    if owns:
        conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO skill_episode
                    (tenant_id, source, work_item_id, pattern_key, occurrence_n, provenance)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (tenant_id, source, work_item_id, pattern_key, occurrence_n, Json(provenance or {})),
            )
            episode_id = cur.fetchone()[0]
    finally:
        if owns:
            conn.close()
    return episode_id


def pattern_occurrence_counts(tenant_id: str, *, conn=None) -> dict[str, int]:
    """SUM(occurrence_n) per pattern_key for the tenant. The deterministic basis
    of the materialization threshold."""
    owns = conn is None
    if owns:
        conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pattern_key, COALESCE(SUM(occurrence_n), 0)
                FROM skill_episode
                WHERE tenant_id = %s
                GROUP BY pattern_key
                """,
                (tenant_id,),
            )
            return {r[0]: int(r[1]) for r in cur.fetchall()}
    finally:
        if owns:
            conn.close()


def _candidate_skill_key(pattern_key: str) -> str:
    """Deterministic skill_key for a materialized pattern (P1)."""
    return f"auto-{pattern_key}"


def _next_version(cur, tenant_id: str, skill_key: str) -> int:
    cur.execute(
        "SELECT COALESCE(MAX(version), 0) FROM skill_registry WHERE tenant_id = %s AND skill_key = %s",
        (tenant_id, skill_key),
    )
    return int(cur.fetchone()[0]) + 1


def _has_live_version(cur, tenant_id: str, skill_key: str) -> bool:
    """Does a version of this skill already exist in a 'live' state (not
    rolled_back/retired)? If so, do not re-materialize (pipeline idempotency)."""
    cur.execute(
        """
        SELECT 1 FROM skill_registry
        WHERE tenant_id = %s AND skill_key = %s
          AND status IN ('candidate', 'approved', 'canary', 'active')
        LIMIT 1
        """,
        (tenant_id, skill_key),
    )
    return cur.fetchone() is not None


@dataclass
class MaterializedCandidate:
    tenant_id: str
    skill_key: str
    version: int
    pattern_key: str
    occurrences: int


def _episode_provenance(cur, tenant_id: str, pattern_key: str) -> dict[str, Any]:
    cur.execute(
        """
        SELECT source, COUNT(*), COALESCE(SUM(occurrence_n), 0),
               COALESCE(array_agg(DISTINCT work_item_id) FILTER (WHERE work_item_id IS NOT NULL), '{}')
        FROM skill_episode
        WHERE tenant_id = %s AND pattern_key = %s
        GROUP BY source
        """,
        (tenant_id, pattern_key),
    )
    by_source: dict[str, Any] = {}
    work_items: set[str] = set()
    for source, n_rows, sum_occ, wi in cur.fetchall():
        by_source[source] = {"episodes": int(n_rows), "occurrences": int(sum_occ)}
        work_items.update(wi or [])
    return {
        "materialized_from": "skill_episode",
        "pattern_key": pattern_key,
        "by_source": by_source,
        "work_items": sorted(work_items),
    }


def materialize_candidates(
    tenant_id: str,
    *,
    threshold: int | None = None,
    conn=None,
    body_builder=None,
) -> list[MaterializedCandidate]:
    """WSC-E4-T2. For every pattern_key with occurrences >= threshold that has no
    live version yet, materialize a CANDIDATE skill (status='candidate',
    incremented version, provenance from the episodes). 100% deterministic — the
    body is a template (`body_builder`, injectable), NEVER LLM-generated here.
    Idempotent: running it again does not duplicate candidates.

    created_by = 'system:skill-promotion' on purpose: a candidate is the
    machine's PROPOSAL; it only becomes servable after an eval + human approval
    (P3). (Migration 0010's "created_by is never system:*" rule applies to the
    seeds already human-APPROVED, not to candidates proposed by the pipeline.)"""
    thr = DEFAULT_CANDIDATE_THRESHOLD if threshold is None else threshold
    builder = body_builder or _default_candidate_body

    owns = conn is None
    if owns:
        conn = _connect()
    created: list[MaterializedCandidate] = []
    try:
        counts = pattern_occurrence_counts(tenant_id, conn=conn)
        with conn, conn.cursor() as cur:
            for pattern_key in sorted(counts):
                occ = counts[pattern_key]
                if occ < thr:
                    continue
                skill_key = _candidate_skill_key(pattern_key)
                if _has_live_version(cur, tenant_id, skill_key):
                    continue
                version = _next_version(cur, tenant_id, skill_key)
                prov = _episode_provenance(cur, tenant_id, pattern_key)
                prov["occurrences_at_materialization"] = occ
                title, body, category, applies_to = builder(pattern_key, prov)
                cur.execute(
                    """
                    INSERT INTO skill_registry
                        (tenant_id, skill_key, title, body, category, applies_to,
                         status, created_by, version, pattern_key, provenance)
                    VALUES (%s, %s, %s, %s, %s, %s, 'candidate', 'system:skill-promotion', %s, %s, %s)
                    """,
                    (
                        tenant_id, skill_key, title, body, category, Json(applies_to),
                        version, pattern_key, Json(prov),
                    ),
                )
                audit_emit(
                    actor="system:skill-promotion",
                    action="skill_candidate_materialized",
                    tenant_id=tenant_id,
                    details={
                        "skill_key": skill_key,
                        "version": version,
                        "pattern_key": pattern_key,
                        "occurrences": occ,
                        "threshold": thr,
                        "provenance": prov,
                    },
                    conn=conn,
                )
                created.append(
                    MaterializedCandidate(tenant_id, skill_key, version, pattern_key, occ)
                )
    finally:
        if owns:
            conn.close()
    return created


def _default_candidate_body(pattern_key: str, provenance: dict[str, Any]):
    """Deterministic template for the candidate's body (P1). Returns
    (title, body, category, applies_to)."""
    sources = ", ".join(sorted(provenance.get("by_source", {}).keys())) or "unknown"
    title = f"Recurring pattern: {pattern_key}"
    body = (
        f"CANDIDATE skill materialized automatically from pattern_key "
        f"'{pattern_key}' (sources: {sources}). Requires eval + human approval "
        f"before being served to the Planner. Full provenance in provenance."
    )
    return title, body, "auto", ["default"]


# ===========================================================================
# WSC-E4-T3 — candidate eval (replay against the historical eval set)
# ===========================================================================
@dataclass
class EvalCase:
    """One eval-set case. `label` = 'positive' (the skill SHOULD help/fire) or
    'negative' (the skill must NOT fire). `pattern_key`/`text` describe the case
    — the deterministic matcher decides whether the skill fires."""

    label: str  # 'positive' | 'negative'
    pattern_key: str
    text: str = ""


@dataclass
class EvalOutcome:
    passed: bool
    score: float
    positive_hits: int
    negative_regressions: int
    detail: str
    n_positive: int = 0
    n_negative: int = 0


def build_eval_set_from_episodes(
    tenant_id: str, candidate_pattern_key: str, *, conn=None
) -> list[EvalCase]:
    """Build an eval set from the tenant's episodes:
      - POSITIVES: episodes with the SAME pattern_key as the candidate (cases
        where the skill would help — it must fire);
      - NEGATIVES: episodes from OTHER pattern_keys (cases where the skill must
        NOT fire — if it does, that is a regression).
    Deterministic and tenant-scoped."""
    owns = conn is None
    if owns:
        conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pattern_key, COALESCE(provenance->>'text', '') "
                "FROM skill_episode WHERE tenant_id = %s",
                (tenant_id,),
            )
            rows = cur.fetchall()
    finally:
        if owns:
            conn.close()
    cases: list[EvalCase] = []
    for pk, text in rows:
        label = "positive" if pk == candidate_pattern_key else "negative"
        cases.append(EvalCase(label=label, pattern_key=pk, text=text or ""))
    return cases


def _skill_fires(candidate_pattern_key: str, case: EvalCase) -> bool:
    """Deterministic matcher: the skill materialized from `candidate_pattern_key`
    fires on a case whose pattern_key is equal. (A simple, auditable trigger;
    replaceable by a richer matcher behind this same function.)"""
    return case.pattern_key == candidate_pattern_key


def evaluate_candidate(
    tenant_id: str,
    skill_key: str,
    candidate_version: int,
    *,
    conn=None,
    eval_cases: list[EvalCase] | None = None,
    write_row: bool = True,
) -> EvalOutcome:
    """Replay the candidate against the eval set (positives + negatives),
    deterministically. `negative_regressions > 0` ⇒ `passed=False` (blocks
    promotion by construction). Writes the trail to `skill_eval` (P8)."""
    owns = conn is None
    if owns:
        conn = _connect()
    try:
        # The candidate's pattern_key (source of truth: the materialized row).
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pattern_key FROM skill_registry "
                "WHERE tenant_id = %s AND skill_key = %s AND version = %s",
                (tenant_id, skill_key, candidate_version),
            )
            row = cur.fetchone()
        if row is None:
            raise SkillNotFound(f"{tenant_id}/{skill_key} v{candidate_version} does not exist")
        candidate_pattern_key = row[0] or skill_key

        cases = (
            eval_cases
            if eval_cases is not None
            else build_eval_set_from_episodes(tenant_id, candidate_pattern_key, conn=conn)
        )
        n_pos = sum(1 for c in cases if c.label == "positive")
        n_neg = sum(1 for c in cases if c.label == "negative")

        positive_hits = 0
        negative_regressions = 0
        for c in cases:
            fired = _skill_fires(candidate_pattern_key, c)
            if c.label == "positive" and fired:
                positive_hits += 1
            elif c.label == "negative" and fired:
                negative_regressions += 1

        # score = recall over the positives (0..1); informational only. The GATE
        # is negative_regressions == 0 AND at least one positive_hit.
        score = (positive_hits / n_pos) if n_pos else 0.0
        passed = negative_regressions == 0 and positive_hits > 0
        detail = (
            f"positives={n_pos} negatives={n_neg} hits={positive_hits} "
            f"regressions={negative_regressions} score={score:.3f}"
        )

        if write_row:
            with conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO skill_eval
                        (tenant_id, skill_key, candidate_version, passed, score,
                         positive_hits, negative_regressions, detail)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (tenant_id, skill_key, candidate_version, passed, score,
                     positive_hits, negative_regressions, detail),
                )
            audit_emit(
                actor="system:skill-promotion",
                action="skill_candidate_evaluated",
                tenant_id=tenant_id,
                details={
                    "skill_key": skill_key,
                    "candidate_version": candidate_version,
                    "passed": passed,
                    "positive_hits": positive_hits,
                    "negative_regressions": negative_regressions,
                    "score": round(score, 6),
                },
                conn=conn,
            )
    finally:
        if owns:
            conn.close()

    return EvalOutcome(
        passed=passed,
        score=score,
        positive_hits=positive_hits,
        negative_regressions=negative_regressions,
        detail=detail,
        n_positive=n_pos,
        n_negative=n_neg,
    )


# ===========================================================================
# WSC-E4-T3 — governed promotion (state machine + pointer rollback)
# ===========================================================================
@dataclass
class TransitionOutcome:
    skill_key: str
    version: int
    from_status: str
    to_status: str
    restored_version: int | None = None  # version restored to active on rollback
    superseded_version: int | None = None  # previously served version demoted on activation


def _current_status(cur, tenant_id: str, skill_key: str, version: int) -> str:
    cur.execute(
        "SELECT status FROM skill_registry "
        "WHERE tenant_id = %s AND skill_key = %s AND version = %s FOR UPDATE",
        (tenant_id, skill_key, version),
    )
    row = cur.fetchone()
    if row is None:
        raise SkillNotFound(f"{tenant_id}/{skill_key} v{version} does not exist")
    return row[0]


def _has_passing_eval(cur, tenant_id: str, skill_key: str, version: int) -> bool:
    cur.execute(
        """
        SELECT 1 FROM skill_eval
        WHERE tenant_id = %s AND skill_key = %s AND candidate_version = %s
          AND passed = TRUE AND negative_regressions = 0
        LIMIT 1
        """,
        (tenant_id, skill_key, version),
    )
    return cur.fetchone() is not None


def _served_version(cur, tenant_id: str, skill_key: str, exclude_version: int) -> int | None:
    """The skill's currently SERVED version (approved/active), if any, other than
    `exclude_version`. There is at most one (partial unique index)."""
    cur.execute(
        """
        SELECT version FROM skill_registry
        WHERE tenant_id = %s AND skill_key = %s AND version <> %s
          AND status IN ('approved', 'active')
        ORDER BY version DESC
        LIMIT 1
        FOR UPDATE
        """,
        (tenant_id, skill_key, exclude_version),
    )
    row = cur.fetchone()
    return int(row[0]) if row else None


def _set_status(cur, tenant_id: str, skill_key: str, version: int, status: str) -> None:
    cur.execute(
        "UPDATE skill_registry SET status = %s "
        "WHERE tenant_id = %s AND skill_key = %s AND version = %s",
        (status, tenant_id, skill_key, version),
    )


def promote(
    tenant_id: str,
    skill_key: str,
    version: int,
    to_status: str,
    *,
    approver: str | None = None,
    reason: str = "",
    conn=None,
) -> TransitionOutcome:
    """GOVERNED state transition (WSC-E4-T3). One transaction; the write order
    respects the "one served version" partial unique index.

    Invariants by construction (they raise BEFORE writing):
      - to_status in {approved, active} without a human approver ⇒ ApproverRequired.
      - candidate → approved without a passing eval ⇒ EvalGateNotPassed.
      - a transition outside ALLOWED_TRANSITIONS ⇒ IllegalTransition.

    Rollback (to_status='rolled_back'): the version becomes rolled_back and the
    previously served version (recorded in provenance.supersedes) goes back to
    active — a POINTER change in seconds (failure mode 13)."""
    # --- P1/P3 gate: human approver BEFORE any write. ---
    if to_status in APPROVER_REQUIRED_TO and not _is_human_principal(approver):
        raise ApproverRequired(
            f"promotion to '{to_status}' requires a named human approver "
            f"(received: {approver!r}) — P1/P3, impossible by construction"
        )

    owns = conn is None
    if owns:
        conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            from_status = _current_status(cur, tenant_id, skill_key, version)

            allowed = ALLOWED_TRANSITIONS.get(from_status, set())
            if to_status not in allowed:
                raise IllegalTransition(
                    f"{skill_key} v{version}: transition {from_status!r} -> {to_status!r} "
                    f"not allowed (allowed: {sorted(allowed)})"
                )

            if from_status == "candidate" and to_status == "approved":
                if not _has_passing_eval(cur, tenant_id, skill_key, version):
                    raise EvalGateNotPassed(
                        f"{skill_key} v{version}: candidate -> approved blocked — "
                        f"no passing eval (negative_regressions=0) recorded"
                    )

            outcome = TransitionOutcome(
                skill_key=skill_key, version=version,
                from_status=from_status, to_status=to_status,
            )

            if to_status in SERVED_STATUSES:
                # Entering the SERVED set ({approved, active}): demote the
                # previously served version (if any, and if it is a different
                # one) BEFORE serving this one — the partial unique index
                # `uq_..._one_served` never sees two served versions of the same
                # skill. The superseded version becomes 'retired' and is recorded
                # in provenance.supersedes so the rollback can hand the pointer
                # back.
                prev = _served_version(cur, tenant_id, skill_key, exclude_version=version)
                if prev is not None:
                    _set_status(cur, tenant_id, skill_key, prev, "retired")
                    outcome.superseded_version = prev
                    cur.execute(
                        "UPDATE skill_registry "
                        "SET provenance = provenance || %s::jsonb "
                        "WHERE tenant_id = %s AND skill_key = %s AND version = %s",
                        (Json({"supersedes": prev}), tenant_id, skill_key, version),
                    )
                _set_status(cur, tenant_id, skill_key, version, to_status)

            elif to_status == "rolled_back":
                # Pointer rollback: first take this one out of served, then
                # restore the version it superseded (if any) to active.
                _set_status(cur, tenant_id, skill_key, version, "rolled_back")
                cur.execute(
                    "SELECT provenance->>'supersedes' "
                    "FROM skill_registry WHERE tenant_id = %s AND skill_key = %s AND version = %s",
                    (tenant_id, skill_key, version),
                )
                sup = cur.fetchone()[0]
                if sup is not None:
                    restored = int(sup)
                    # Only restore if the superseded version still exists and is
                    # demoted (retired) — it becomes the served pointer again.
                    cur.execute(
                        "SELECT status FROM skill_registry "
                        "WHERE tenant_id = %s AND skill_key = %s AND version = %s FOR UPDATE",
                        (tenant_id, skill_key, restored),
                    )
                    r = cur.fetchone()
                    if r is not None and r[0] in ("retired", "rolled_back", "approved"):
                        _set_status(cur, tenant_id, skill_key, restored, "active")
                        outcome.restored_version = restored
            else:
                # approved / canary: a plain status transition.
                _set_status(cur, tenant_id, skill_key, version, to_status)

            audit_emit(
                actor=approver if _is_human_principal(approver) else "system:skill-promotion",
                action="skill_promotion_transition",
                tenant_id=tenant_id,
                details={
                    "skill_key": skill_key,
                    "version": version,
                    "from_status": from_status,
                    "to_status": to_status,
                    "approver": approver,
                    "reason": reason,
                    "restored_version": outcome.restored_version,
                    "superseded_version": outcome.superseded_version,
                },
                conn=conn,
            )
    finally:
        if owns:
            conn.close()
    return outcome
