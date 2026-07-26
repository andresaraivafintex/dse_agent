"""Deterministic repository resolution cascade (Report 07 C2 / Phase B).

Slack/Jira tasks are not born in a repo (unlike GitHub, where the issue is
ALREADY in the repo). This cascade discovers the repo from the MOST
specific/explicit to the least and — crucially — returns `(None, None)` when
there is no trustworthy resolution, so the workflow ASKS instead of guessing.
The wrong repo never happens silently (fail-safe; P1: config + regex only, no
LLM).

Order:
  1. explicit override (`repo=owner/name` in the text / dedicated field)
  2. specific binding: channel (Slack) / component (Jira) / project (Jira)
  3. broad binding: workspace (Slack team / Jira site)
  4. the tenant's single-repo default (binding_value '*')
  5. nothing -> (None, None) -> clarification
"""
from __future__ import annotations

import re
from typing import Any

# explicit override in free text (same format as C4).
_RE_REPO = re.compile(r"\brepo(?:sitory)?\s*[:=]\s*([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)", re.I)
_RE_BRANCH = re.compile(r"\b(?:base[_-]?)?branch\s*[:=]\s*([A-Za-z0-9._/-]+)", re.I)

# precedence: lower index = more specific.
_TYPE_PRECEDENCE = ("channel", "component", "project", "workspace")


def parse_explicit_repo(text: str) -> tuple[str | None, str | None]:
    """(repo, base_branch) explicitly given in the text, if any. Rung 1 of the cascade."""
    if not text:
        return None, None
    m = _RE_REPO.search(text)
    b = _RE_BRANCH.search(text)
    return (m.group(1) if m else None), (b.group(1) if b else None)


def resolve_repo(
    conn,
    *,
    tenant_id: str,
    platform: str,
    signals: dict[str, Any],
) -> tuple[str | None, str | None]:
    """Runs the cascade. `signals` carries whatever the adapter managed to
    extract: {text, channel, component, project, workspace}. Returns (repo,
    base_branch) or (None, None) if nothing resolves (=> the workflow asks).

    GitHub does not use this (the repo comes from the webhook); kept generic
    for symmetry.
    """
    # Rung 1 — an explicit override beats everything.
    repo, branch = parse_explicit_repo(signals.get("text") or "")
    if repo:
        return repo, (branch or "main")

    # Rungs 2-3 — bindings, from the most specific to the broadest.
    candidates: list[tuple[str, str]] = []  # (binding_type, binding_value)
    for btype in _TYPE_PRECEDENCE:
        val = signals.get(btype)
        if val:
            candidates.append((btype, str(val)))
    if candidates:
        with conn.cursor() as cur:
            for btype, val in candidates:  # already in precedence order
                cur.execute(
                    "SELECT repo, base_branch FROM repo_bindings "
                    "WHERE tenant_id = %s AND platform = %s AND binding_type = %s "
                    "AND binding_value = %s",
                    (tenant_id, platform, btype, val),
                )
                row = cur.fetchone()
                if row:
                    return row[0], row[1]

    # Rung 4 — the tenant's single-repo default (binding_value '*'), any
    # binding_type; if the tenant has EXACTLY one distinct repo, use it.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT repo, base_branch FROM repo_bindings WHERE tenant_id = %s",
            (tenant_id,),
        )
        rows = cur.fetchall()
    distinct_repos = {r[0] for r in rows}
    if len(distinct_repos) == 1:
        only = rows[0]
        return only[0], only[1]

    # Rung 5 — ambiguous or empty: no trustworthy resolution, so ask (never
    # guesses among multiple repos).
    return None, None
