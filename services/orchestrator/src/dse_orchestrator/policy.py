"""WSB-E3-T2/T3 — the plan approval gate POLICY, deliberately OUTSIDE the model
(P1: no flow decision by an LLM). Two responsibilities, both pure and
deterministic:

1. `classify_risk(plan, declared_risk)` — the EFFECTIVE risk class. It starts
   from the class declared by the Planner (WS-C) but runs its own
   defense-in-depth classification: a plan that touches migrations, CI
   workflows, or forbidden paths is ALWAYS `high`, even if the Planner said
   `low` (a model that under-classifies must not weaken the gate — P1). The
   statement's high-risk categories (auth/money/data model/migration/cross-repo)
   are mapped by deterministic signals in the plan's declared paths.

2. `requires_plan_approval(risk_class, policy)` — given the effective class and
   the POLICY (the set of classes that require approval, coming from config,
   never from the model), decides whether to park at the gate or auto-approve.

The module does NO I/O — approver resolution (which needs DB/GitHub) lives in an
Activity (`local_activities.resolve_plan_approver`), never in the workflow's
deterministic body.
"""
from __future__ import annotations

from typing import Iterable

# Deterministic high-risk signals by path prefix/substring. Mirrors (and
# extends) the PlanArtifact default `forbidden_paths`. It is not a model: it is
# an explicit, auditable allowlist/denylist, versioned in code.
_HIGH_RISK_PATH_MARKERS: tuple[str, ...] = (
    "migrations/",          # data model / migration
    ".github/workflows/",   # CI/CD supply chain
    "auth/",                # authentication/authorization
    "authz/",
    "billing/",             # money
    "payments/",
    "payment/",
    "iam/",
    "secrets",
)


def classify_risk(
    expected_files: Iterable[str] | None,
    declared_risk: str | None,
    *,
    cross_repo: bool = False,
) -> str:
    """Effective risk class. `high` dominates: if any deterministic high-risk
    signal is present, the result is `high` regardless of what the Planner
    declared. Otherwise it respects the declared class (default `low`)."""
    files = list(expected_files or [])
    for path in files:
        p = (path or "").lower()
        for marker in _HIGH_RISK_PATH_MARKERS:
            if marker in p:
                return "high"
    if cross_repo:
        return "high"
    declared = (declared_risk or "low").strip().lower()
    if declared in ("high", "critical"):
        return "high"
    return declared or "low"


def requires_plan_approval(risk_class: str, require_classes: Iterable[str]) -> bool:
    """P1: a purely deterministic policy decision. `require_classes` comes from
    the operator's config (WSB `require_approval_risk_classes`), NEVER from a
    model."""
    normalized = {c.strip().lower() for c in require_classes}
    return (risk_class or "low").strip().lower() in normalized


# ---------------------------------------------------------------------------
# Approver resolution cascade (WSB-E3-T2): CODEOWNERS -> designated approvers
# from the access bundle (WS-F). These objects run INSIDE an Activity
# (`resolve_plan_approver`) — they need I/O (read CODEOWNERS via the GitHub
# adapter / query `dse_access_bundles`), so they are never called from the
# workflow body.
# ---------------------------------------------------------------------------

# Swappable CODEOWNERS reader. Production: the GitHub adapter (WS-A) reads the
# repo's CODEOWNERS file via the GitHub App. Here it is an injection point:
# the default returns None (no local GitHub integration) — documented as a gap
# in the README. Signature: (tenant_id, repo) -> CODEOWNERS text or None.
_codeowners_reader = None  # type: ignore[var-annotated]


def set_codeowners_reader(fn) -> None:
    """Injects the CODEOWNERS reader (production: GitHub adapter; tests: fake)."""
    global _codeowners_reader
    _codeowners_reader = fn


def parse_codeowners_owners(text: str | None) -> list[str]:
    """Extracts the flattened list of owners (logins/@teams) from a CODEOWNERS
    file. Deterministic: no per-path glob matching in Phase 2 (the plan gate is
    per WorkItem, not per file) — returns the UNION of owners across all rules,
    deduplicated while preserving order. Comment/blank lines are ignored."""
    if not text:
        return []
    owners: list[str] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # format: <pattern> <owner1> <owner2> ...
        parts = line.split()
        for token in parts[1:]:
            if token.startswith("@") or "@" in token:  # @user, @org/team, email
                if token not in seen:
                    seen.add(token)
                    owners.append(token)
    return owners
