"""Materialization of skills into the sandbox workspace (console ⇄ engine
integration).

The console (dse_console_pane) is the central skill store; the per-repo
checkbox (`skill_registry.repo_scope`, migration 0029) decides what runs where.
This module writes the served skills as Claude-Agent-Skill files in the
workspace — `.claude/skills/<skill_key>/SKILL.md` — BEFORE the agent turn:

  - `ClaudeAgentSubstrate` loads them natively (`setting_sources=["project"]`
    — project/workspace only, never the host user's settings);
  - any other substrate reaches them by reading files, guided by the
    instruction note (`workspace_skills_note`).

Deliberate rules:
  - a skill already COMMITTED in the target repo (existing
    `.claude/skills/<key>/`) BEATS the registry version — a convention
    versioned alongside the code is sovereign; nothing gets overwritten.
  - everything we materialize goes into `.git/info/exclude` (a LOCAL exclude,
    never committed) — the Coder's deterministic commit (`ScopedGitSession`)
    never drags guidance into the PR.
"""
from __future__ import annotations

import re
from pathlib import Path

from .skill_registry import Skill

_SKILLS_SUBDIR = Path(".claude") / "skills"


def _safe_key(skill_key: str) -> str:
    """Directory name derived from the skill_key — never a path traversal."""
    key = re.sub(r"[^a-zA-Z0-9._-]", "-", skill_key).strip(".-") or "skill"
    return key[:64]


def _frontmatter(skill: Skill) -> str:
    description = " ".join(skill.title.split()) or skill.skill_key
    return f"---\nname: {_safe_key(skill.skill_key)}\ndescription: {description}\n---\n\n"


def _git_exclude(workspace_dir: Path, rel_paths: list[str]) -> None:
    """Register the materialized paths in the clone's LOCAL exclude (this is not
    the repo's .gitignore — it never becomes a diff). With no git repo
    (tests/fixtures), no-op."""
    info_dir = workspace_dir / ".git" / "info"
    if not (workspace_dir / ".git").is_dir():
        return
    info_dir.mkdir(parents=True, exist_ok=True)
    exclude = info_dir / "exclude"
    existing = exclude.read_text() if exclude.is_file() else ""
    lines = [p for p in rel_paths if p not in existing]
    if lines:
        exclude.write_text(existing.rstrip("\n") + ("\n" if existing else "") + "\n".join(lines) + "\n")


def materialize_skills(workspace_dir: str, skills: list[Skill]) -> list[str]:
    """Write each served skill to the workspace's `.claude/skills/<key>/SKILL.md`.
    Idempotent; returns the keys actually materialized (skills already present
    in the target repo are skipped — repo beats registry).

    GUARD (found in the real 2026-07-23 run, wi_17eefa): only materialize into
    an ALREADY PROVISIONED (git) workspace. Creating the directory before the
    clone made `provision_sandbox` skip the clone (`if not exists`) and the
    checkpoint died with "not a git directory". No `.git` => no-op."""
    ws = Path(workspace_dir)
    if not (ws / ".git").exists():
        return []
    written: list[str] = []
    excluded: list[str] = []
    for skill in skills:
        key = _safe_key(skill.skill_key)
        target_dir = ws / _SKILLS_SUBDIR / key
        target = target_dir / "SKILL.md"
        rel = f"{_SKILLS_SUBDIR.as_posix()}/{key}/"
        if target.is_file() and rel not in _materialized_marker(ws):
            # A pre-existing SKILL.md that we did NOT materialize = committed in
            # the target repo — sovereign, do not overwrite.
            continue
        target_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(_frontmatter(skill) + skill.body, encoding="utf-8")
        written.append(skill.skill_key)
        excluded.append(rel)
    if written:
        _git_exclude(ws, excluded)
        _write_materialized_marker(ws, excluded)
    return written


# Provenance marker: distinguishes "a skill WE wrote in a previous run"
# (safe to rewrite/update) from "a skill committed in the repo" (untouchable).
_MARKER = ".claude/.dse-materialized"


def _materialized_marker(ws: Path) -> set[str]:
    p = ws / _MARKER
    if not p.is_file():
        return set()
    return {line.strip() for line in p.read_text().splitlines() if line.strip()}


def _write_materialized_marker(ws: Path, rels: list[str]) -> None:
    p = ws / _MARKER
    p.parent.mkdir(parents=True, exist_ok=True)
    merged = sorted(_materialized_marker(ws) | set(rels))
    p.write_text("\n".join(merged) + "\n")
    _git_exclude(ws, [_MARKER])


def workspace_skills_note(workspace_dir: str) -> str:
    """Instruction section pointing at the skills present in the workspace (both
    the ones materialized from the registry AND the ones committed in the repo).
    Empty when there are none — the prompt gains no noise."""
    ws = Path(workspace_dir) / _SKILLS_SUBDIR
    if not ws.is_dir():
        return ""
    entries: list[str] = []
    for skill_dir in sorted(ws.iterdir()):
        md = skill_dir / "SKILL.md"
        if not md.is_file():
            continue
        description = ""
        try:
            for line in md.read_text(encoding="utf-8").splitlines()[:8]:
                if line.startswith("description:"):
                    description = line.removeprefix("description:").strip()
                    break
        except (OSError, UnicodeDecodeError):
            continue
        entries.append(f"- .claude/skills/{skill_dir.name}/SKILL.md — {description or skill_dir.name}")
    if not entries:
        return ""
    return (
        "\n\n## Repository skills (MANDATORY guidance)\n"
        "Before writing code, read each SKILL.md below and follow its rules:\n"
        + "\n".join(entries)
    )
