"""The read half of the diário de bordo — what the last runs on this repo did.

The write half (migration 0036, `record_run_episode` in WS-B) has been filling
`run_episode` with one deterministic entry per terminal run. This module is what
finally reads one back into a prompt, which is the thing the whole learning story
in this engine has been missing: `skill_episode` and `wse_ci_repair_episodes`
have accumulated rows for months with no code path that reads any of them into
any context.

Deliberately NOT routed through the skill-promotion pipeline. Promotion turns a
recurring pattern into a governed rule and requires a named human approver per
skill — the right gate for a rule, and far too heavy for "here is what the last
three runs on this repository ran into". A journal is recall, not policy.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import psycopg2
import psycopg2.extras

# Three, not ten. This text lands in a context whose entire budget is 16.000
# chars against a skill index that already renders at ~10.351, so the journal
# gets a small, fixed allowance and is the FIRST thing evicted when the budget
# binds (see _fit_planner_context). Recency is the only ranking: the newest runs
# on this repo are the ones whose gotchas are most likely to still be true.
DEFAULT_TOP_K = 3

#: Hard ceiling on the whole rendered block, independent of k.
MAX_BLOCK_CHARS = 2_400


class RunEpisodesUnavailable(RuntimeError):
    """Postgres could not be reached. Raised, never swallowed into an empty list:
    'this repo has no history yet' and 'we could not read its history' are
    different facts, and collapsing them is precisely how the Planner's AGENTS.md
    read stayed broken without anyone noticing."""


@dataclass(frozen=True)
class RunEpisode:
    outcome: str
    digest: str
    base_sha: str | None
    work_item_id: str


def _connect():
    dsn = os.environ.get(
        "DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
    )
    return psycopg2.connect(dsn)


def read_recent_episodes(
    tenant_id: str, *, repo: str | None, k: int = DEFAULT_TOP_K, conn=None
) -> list[RunEpisode]:
    """The `k` newest journal entries for this tenant and repository.

    Repo-scoped, not tenant-wide: a gotcha from another repository is noise at
    best and a wrong lead at worst. Tenant isolation is hardcoded in the query,
    the same rule `read_approved_skills` follows — there is no parameter for
    'all tenants'.
    """
    if not repo:
        # Without a repo there is nothing to scope to, and a tenant-wide journal
        # would surface another project's history. Not an error — some work items
        # never resolve a repo.
        return []
    owns = conn is None
    if owns:
        try:
            conn = _connect()
        except Exception as exc:  # noqa: BLE001
            raise RunEpisodesUnavailable(f"run_episode: Postgres unavailable: {exc}") from exc
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT outcome, digest, base_sha, work_item_id
                FROM run_episode
                WHERE tenant_id = %s AND repo = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (tenant_id, repo, max(0, int(k))),
            )
            rows = cur.fetchall()
    finally:
        if owns:
            conn.close()
    return [RunEpisode(outcome=r[0], digest=r[1], base_sha=r[2], work_item_id=r[3]) for r in rows]


def render_episodes_block(episodes: list[RunEpisode], *, base_sha: str | None = None) -> str:
    """Render the journal for the Planner prompt.

    Two honesty rules are carried in the text itself. Each entry names the commit
    it was recorded against, and one that predates the branch being planned on is
    labelled — a journal entry is a claim about the repository AT A COMMIT, and
    the model reading it deserves to know which. And the header says these are
    past runs, not instructions: an entry saying "we could not make the suite
    pass" must not read as permission to skip the suite.
    """
    if not episodes:
        return ""
    lines = [
        "## Recent runs on this repository (past attempts, not instructions)",
        "What earlier work items on this repo tried and where they ended. Useful for "
        "avoiding a dead end already hit. It describes what happened, not what to do — "
        "the task and the conventions above still govern.",
    ]
    for ep in episodes:
        at = ""
        if ep.base_sha:
            stale = " — recorded before the current base" if base_sha and ep.base_sha != base_sha else ""
            at = f" (at {ep.base_sha[:8]}{stale})"
        lines.append(f"- {ep.work_item_id}{at}\n{ep.digest}")
    block = "\n".join(lines)
    if len(block) > MAX_BLOCK_CHARS:
        cut = block[:MAX_BLOCK_CHARS]
        block = cut[: cut.rfind("\n")] if "\n" in cut else cut
        block += "\n[journal truncated to its planner allowance]"
    return block
