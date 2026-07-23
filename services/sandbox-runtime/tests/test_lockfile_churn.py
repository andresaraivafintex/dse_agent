"""Guard-rails do 2º disparo real (2026-07-22): churn de lockfile e
idempotência de autoria do Tester.

O run wi_bacdce7 reprovou APENAS em diff_budget porque o npm reescreveu 16
linhas de package-lock.json ao rodar os testes (sem mudança de dependência) e
o commit determinístico levou o churn para o diff. No fix cycle, o Tester
autorava um teste NOVO a cada ciclo — o diff só crescia e o loop não convergia.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from sandbox_runtime.activities import (
    _restore_lockfile_churn,
    _tester_authored_files_in_history,
)


def _git(ws: str, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ws, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture()
def repo(tmp_path):
    ws = str(tmp_path / "ws")
    os.makedirs(ws)
    _git(ws, "init", "-q", "-b", "main")
    _git(ws, "config", "user.email", "t@dse.local")
    _git(ws, "config", "user.name", "t")
    with open(os.path.join(ws, "package.json"), "w") as fh:
        fh.write('{"name": "app", "dependencies": {}}\n')
    with open(os.path.join(ws, "package-lock.json"), "w") as fh:
        fh.write('{"lockfileVersion": 3}\n')
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", "base")
    return ws


def test_lockfile_churn_sem_manifesto_e_restaurado(repo):
    with open(os.path.join(repo, "package-lock.json"), "a") as fh:
        fh.write('/* churn do npm */\n')
    restored = _restore_lockfile_churn(repo)
    assert restored == ["package-lock.json"]
    assert _git(repo, "status", "--porcelain").strip() == ""


def test_lockfile_com_manifesto_mudado_fica(repo):
    # dependência declarada mudou junto: o par lockfile+manifesto é legítimo
    with open(os.path.join(repo, "package.json"), "w") as fh:
        fh.write('{"name": "app", "dependencies": {"left-pad": "^1.0.0"}}\n')
    with open(os.path.join(repo, "package-lock.json"), "a") as fh:
        fh.write('/* resolução nova */\n')
    assert _restore_lockfile_churn(repo) == []
    porcelain = _git(repo, "status", "--porcelain")
    assert "package-lock.json" in porcelain and "package.json" in porcelain


def test_lockfile_novo_untracked_sem_manifesto_e_removido(repo):
    # ex.: repo sem yarn.lock; rodar yarn cria um do nada
    with open(os.path.join(repo, "yarn.lock"), "w") as fh:
        fh.write("# gerado\n")
    assert _restore_lockfile_churn(repo) == ["yarn.lock"]
    assert not os.path.exists(os.path.join(repo, "yarn.lock"))


def test_arquivo_normal_modificado_nunca_e_tocado(repo):
    src = os.path.join(repo, "app.js")
    with open(src, "w") as fh:
        fh.write("// fix\n")
    assert _restore_lockfile_churn(repo) == []
    assert os.path.exists(src)


def test_tester_reusa_testes_de_commits_anteriores(repo):
    os.makedirs(os.path.join(repo, "test"))
    with open(os.path.join(repo, "test", "delete.test.js"), "w") as fh:
        fh.write("// teste autorado\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "tester(wi_x): cobre remoção por id")
    # commit de coder no meio não conta
    with open(os.path.join(repo, "app.js"), "w") as fh:
        fh.write("// fix\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "coder(wi_x): fix remove")
    assert _tester_authored_files_in_history(repo) == ["test/delete.test.js"]


def test_tester_sem_commits_anteriores_autora_normalmente(repo):
    assert _tester_authored_files_in_history(repo) == []


def test_tester_ignora_arquivo_autorado_que_sumiu(repo):
    os.makedirs(os.path.join(repo, "test"))
    p = os.path.join(repo, "test", "old.test.js")
    with open(p, "w") as fh:
        fh.write("// velho\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "tester(wi_x): velho")
    _git(repo, "rm", "-q", "test/old.test.js")
    _git(repo, "commit", "-q", "-m", "coder(wi_x): remove teste obsoleto")
    assert _tester_authored_files_in_history(repo) == []
