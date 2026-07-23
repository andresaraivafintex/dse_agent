"""Materialização de skills no workspace do sandbox (integração console ⇄ engine).

O console (dse_console_pane) é o banco central de skills; o tick por repo
(`skill_registry.repo_scope`, migração 0029) decide o que roda onde. Este
módulo escreve as skills servidas como arquivos Claude-Agent-Skill no
workspace — `.claude/skills/<skill_key>/SKILL.md` — ANTES do turno do agente:

  - o `ClaudeAgentSubstrate` as carrega nativamente (`setting_sources=
    ["project"]` — só o projeto/workspace, nunca settings do usuário do host);
  - qualquer outro substrato as alcança por leitura de arquivo, guiado pela
    nota de instrução (`workspace_skills_note`).

Regras deliberadas:
  - skill já COMMITADA no repo alvo (`.claude/skills/<key>/` existente) VENCE a
    versão do registry — convenção versionada com o código é soberana; nada é
    sobrescrito.
  - tudo que materializamos entra em `.git/info/exclude` (exclude LOCAL, nunca
    commitado) — o commit determinístico do Coder (`ScopedGitSession`) jamais
    arrasta guidance para o PR.
"""
from __future__ import annotations

import re
from pathlib import Path

from .skill_registry import Skill

_SKILLS_SUBDIR = Path(".claude") / "skills"


def _safe_key(skill_key: str) -> str:
    """Nome de diretório derivado do skill_key — nunca um path traversal."""
    key = re.sub(r"[^a-zA-Z0-9._-]", "-", skill_key).strip(".-") or "skill"
    return key[:64]


def _frontmatter(skill: Skill) -> str:
    description = " ".join(skill.title.split()) or skill.skill_key
    return f"---\nname: {_safe_key(skill.skill_key)}\ndescription: {description}\n---\n\n"


def _git_exclude(workspace_dir: Path, rel_paths: list[str]) -> None:
    """Registra os paths materializados no exclude LOCAL do clone (não é a
    .gitignore do repo — nunca vira diff). Sem repo git (testes/fixtures), no-op."""
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
    """Escreve cada skill servida em `.claude/skills/<key>/SKILL.md` do
    workspace. Idempotente; devolve as keys efetivamente materializadas
    (skills já presentes no repo alvo são puladas — repo vence registry).

    GUARD (achado do disparo real 2026-07-23, wi_17eefa): só materializa em
    workspace JÁ PROVISIONADO (git). Criar o diretório antes do clone fazia o
    `provision_sandbox` pular o clone (`if not exists`) e o checkpoint morria
    em "not a git directory". Sem `.git` => no-op."""
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
            # SKILL.md pré-existente que NÃO foi materializado por nós =
            # commitado no repo alvo — soberano, não sobrescreve.
            continue
        target_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(_frontmatter(skill) + skill.body, encoding="utf-8")
        written.append(skill.skill_key)
        excluded.append(rel)
    if written:
        _git_exclude(ws, excluded)
        _write_materialized_marker(ws, excluded)
    return written


# Marker de proveniência: distingue "skill que NÓS escrevemos em run anterior"
# (pode re-escrever/atualizar) de "skill commitada no repo" (intocável).
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
    """Seção de instrução apontando as skills presentes no workspace (as
    materializadas do registry E as commitadas no repo). Vazio se não houver
    nenhuma — o prompt não ganha ruído."""
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
