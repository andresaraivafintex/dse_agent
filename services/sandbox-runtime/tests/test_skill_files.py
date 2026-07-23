"""Materialização de skills no workspace (skill_files.py).

Invariantes:
  - SÓ materializa em workspace provisionado (git) — criar o diretório antes
    do clone fazia o provision_sandbox pular o clone (achado wi_17eefa,
    2026-07-23); sem `.git` => no-op;
  - escreve `.claude/skills/<key>/SKILL.md` com frontmatter name/description;
  - skill COMMITADA no repo alvo nunca é sobrescrita (repo vence registry);
  - skill materializada por NÓS em run anterior É atualizada (marker);
  - tudo que materializamos entra em `.git/info/exclude` — nunca vira diff;
  - `workspace_skills_note` lista o que existe e é vazia sem skills.
"""
from __future__ import annotations

import subprocess

from sandbox_runtime.skill_files import materialize_skills, workspace_skills_note
from sandbox_runtime.skill_registry import Skill


def _skill(key: str, body: str = "guidance") -> Skill:
    return Skill(tenant_id="dev", skill_key=key, title=f"Título {key}", body=body, category="general")


def _git_ws(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


def test_non_git_workspace_is_noop(tmp_path):
    """Workspace sem `.git` (pré-provision) => NADA é criado — o clone do
    provision_sandbox depende do diretório não existir/ficar intacto."""
    written = materialize_skills(str(tmp_path / "inexistente"), [_skill("x")])
    assert written == []
    assert not (tmp_path / "inexistente").exists()

    (tmp_path / "vazio").mkdir()
    assert materialize_skills(str(tmp_path / "vazio"), [_skill("x")]) == []
    assert list((tmp_path / "vazio").iterdir()) == []


def test_materialize_writes_skill_md(tmp_path):
    ws = _git_ws(tmp_path)
    written = materialize_skills(str(ws), [_skill("handling-money")])
    assert written == ["handling-money"]
    md = ws / ".claude" / "skills" / "handling-money" / "SKILL.md"
    content = md.read_text()
    assert content.startswith("---\nname: handling-money\n")
    assert "description: Título handling-money" in content
    assert content.endswith("guidance")


def test_repo_committed_skill_wins(tmp_path):
    ws = _git_ws(tmp_path)
    committed = ws / ".claude" / "skills" / "repo-own" / "SKILL.md"
    committed.parent.mkdir(parents=True)
    committed.write_text("do repo — soberana")

    written = materialize_skills(str(ws), [_skill("repo-own", body="do registry")])
    assert written == []
    assert committed.read_text() == "do repo — soberana"


def test_rematerialization_updates_our_files(tmp_path):
    ws = _git_ws(tmp_path)
    materialize_skills(str(ws), [_skill("evolving", body="v1")])
    written = materialize_skills(str(ws), [_skill("evolving", body="v2")])
    assert written == ["evolving"]
    md = ws / ".claude" / "skills" / "evolving" / "SKILL.md"
    assert md.read_text().endswith("v2")


def test_git_exclude_keeps_skills_out_of_diff(tmp_path):
    ws = _git_ws(tmp_path)
    (ws / "app.py").write_text("print('x')\n")

    materialize_skills(str(ws), [_skill("guarded")])

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ws, check=True, capture_output=True, text=True
    ).stdout
    assert ".claude" not in status  # excluída localmente — o commit do Coder nunca a arrasta
    assert "app.py" in status


def test_workspace_skills_note(tmp_path):
    ws = _git_ws(tmp_path)
    assert workspace_skills_note(str(ws)) == ""
    materialize_skills(str(ws), [_skill("money"), _skill("pii")])
    note = workspace_skills_note(str(ws))
    assert "MANDATORY guidance" in note
    assert ".claude/skills/money/SKILL.md" in note
    assert ".claude/skills/pii/SKILL.md" in note
