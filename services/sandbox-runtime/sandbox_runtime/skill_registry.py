"""Skill registry bootstrap (WSC-E4-T1).

Tenant-scoped registry of human-curated skills, read by the Planner session
(WSC-E3-T3) to hydrate context. Fase 2 has ONLY the registry + the read path —
the promotion pipeline (automatic skill curation out of executions) is Fase 4,
deliberately out of scope here.

Per-tenant isolation: `read_approved_skills(tenant_id)` only returns rows for
the requested `tenant_id` with `status='approved'` — the Planner never sees
another tenant's skills nor drafts. The `psycopg2` import is mandatory here
(reading skills is part of the Planner's context, not best-effort bookkeeping)
— if Postgres goes down the read FAILS cleanly (P6), it never silently degrades
to "no skills". Use `allow_empty_on_unavailable=True` in test/dev only.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import psycopg2

_DSN = os.environ.get(
    "DSE_SANDBOX_DATABASE_URL",
    os.environ.get("DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"),
)


@dataclass(frozen=True)
class Skill:
    tenant_id: str
    skill_key: str
    title: str
    body: str
    category: str
    applies_to: list[str] = field(default_factory=list)
    # Per-repo checkboxes coming from the console (migration 0029): None =
    # global (native/legacy skill), ["*"] = every repo, ["owner/name", ...] =
    # only those, [] = none (not served to any run).
    repo_scope: list[str] | None = None

    def enabled_for_repo(self, repo: str) -> bool:
        if self.repo_scope is None:
            return True
        return "*" in self.repo_scope or repo in self.repo_scope

    def as_context_block(self) -> str:
        """Deterministic rendering of the skill for the Planner bundle.
        Skills ARE trusted (human-curated) — unlike retrieval content — so they
        go in as guidance, not as untrusted data."""
        applies = ", ".join(self.applies_to) if self.applies_to else "general"
        return f"### skill:{self.skill_key} [{self.category}; applies to: {applies}]\n{self.title}\n{self.body}"


class SkillRegistryUnavailable(Exception):
    """Postgres unavailable while reading the registry — clean failure (P6)."""


def _connect():
    return psycopg2.connect(_DSN)


def read_approved_skills(
    tenant_id: str,
    *,
    task_class: str | None = None,
    repo: str | None = None,
    conn=None,
) -> list[Skill]:
    """The tenant's SERVED skills (the production Planner). If `task_class` is
    given, filters down to the ones that apply to that task_class (or are marked
    'default'); otherwise returns every served skill of the tenant. If `repo` is
    given (owner/name), filters by the console's per-repo checkboxes
    (`repo_scope`, migration 0029): NULL = global, "*" = all, list = membership.

    ISOLATION (coordinated with the WS-F suite): the query hardcodes
    `tenant_id = %s` — there is no path that returns another tenant's skill, and
    no parameter for "all tenants".

    SERVED STATES (Fase 4): `status IN ('approved','active')` — hardcoded.
    `approved` = human-curated/approved (includes the Fase 2 seeds);
    `active` = a candidate that made it through the full pipeline (eval →
    approval → canary → active, WSC-E4-T3). NEVER served: `draft`, `candidate`,
    `canary` (= shadow at this phase), `rolled_back`, `retired`. The partial
    unique index `uq_skill_registry_one_served` structurally guarantees at most
    ONE served version per (tenant, skill_key) — the Planner never sees two
    versions of the same skill.
    """
    owns = conn is None
    if owns:
        try:
            conn = _connect()
        except Exception as exc:  # noqa: BLE001
            raise SkillRegistryUnavailable(f"skill_registry: Postgres unavailable: {exc}") from exc
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT tenant_id, skill_key, title, body, category, applies_to, repo_scope
                FROM skill_registry
                WHERE tenant_id = %s AND status IN ('approved', 'active')
                ORDER BY category, skill_key
                """,
                (tenant_id,),
            )
            rows = cur.fetchall()
    finally:
        if owns:
            conn.close()

    skills = [
        Skill(
            tenant_id=r[0],
            skill_key=r[1],
            title=r[2],
            body=r[3],
            category=r[4],
            applies_to=list(r[5] or []),
            repo_scope=None if r[6] is None else list(r[6]),
        )
        for r in rows
    ]
    if task_class is not None:
        skills = [
            s
            for s in skills
            if not s.applies_to or task_class in s.applies_to or "default" in s.applies_to
        ]
    if repo is not None:
        skills = [s for s in skills if s.enabled_for_repo(repo)]
    return skills
