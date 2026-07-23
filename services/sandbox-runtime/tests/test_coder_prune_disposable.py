"""Prune pós-Coder reconciliado com o L1 advisory (2026-07-22).

Contexto: `expected_files` deixou de reprovar o diff no L1 (é advisory — o
Planner adivinha os arquivos pela issue, antes de ler o código). O prune
determinístico pós-turn, que antes apagava TODO arquivo novo fora do plano,
apagaria agora um arquivo-fonte NOVO e legítimo que o fix precisou criar —
silenciosamente, antes do commit. `_prune_disposable_artifacts` passou a apagar
SÓ lixo óbvio do CLI (relatório/log/scratch); fonte nova legítima sobrevive.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from sandbox_runtime.activities import _prune_disposable_artifacts


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
    with open(os.path.join(ws, "app.js"), "w") as fh:
        fh.write("// base\n")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", "base")
    return ws


def _write(ws: str, rel: str, content: str = "x\n") -> str:
    dest = os.path.join(ws, rel)
    os.makedirs(os.path.dirname(dest) or ws, exist_ok=True)
    with open(dest, "w") as fh:
        fh.write(content)
    return dest


def test_fonte_nova_fica_e_relatorio_espontaneo_e_podado(repo):
    """O caso do enunciado: o fix criou um módulo-fonte NOVO fora do plano e o
    CLI cuspiu um relatório. O módulo tem que sobreviver; o relatório, não."""
    src = _write(repo, "src/novo-modulo.js", "export const fix = 1;\n")
    report = _write(repo, "BUG_FIX_REPORT.md", "# O que eu fiz\n")

    pruned, kept = _prune_disposable_artifacts(
        repo, expected_files=["src/store.js"], work_item_id="wi_x"
    )

    assert os.path.exists(src), "arquivo-fonte novo legítimo NÃO pode ser apagado"
    assert not os.path.exists(report), "relatório espontâneo do CLI deve sumir"
    assert pruned == ["BUG_FIX_REPORT.md"]
    assert kept == ["src/novo-modulo.js"]


def test_multiplos_artefatos_de_lixo_sao_podados(repo):
    for rel in ("run.log", "state.tmp", "app.js.orig", "IMPLEMENTATION_SUMMARY.md"):
        _write(repo, rel)
    _write(repo, "src/feature.py", "def f():\n    return 1\n")

    pruned, kept = _prune_disposable_artifacts(
        repo, expected_files=["src/other.py"], work_item_id="wi_x"
    )

    assert sorted(pruned) == ["IMPLEMENTATION_SUMMARY.md", "app.js.orig", "run.log", "state.tmp"]
    assert kept == ["src/feature.py"]
    assert os.path.exists(os.path.join(repo, "src/feature.py"))


def test_arquivo_no_plano_nunca_e_podado_mesmo_parecendo_lixo(repo):
    """Se o plano PEDIU o arquivo, ele fica — mesmo casando um padrão de lixo."""
    _write(repo, "REPORT.md", "conteúdo pedido pelo plano\n")

    pruned, kept = _prune_disposable_artifacts(
        repo, expected_files=["REPORT.md"], work_item_id="wi_x"
    )

    assert pruned == []
    assert kept == []
    assert os.path.exists(os.path.join(repo, "REPORT.md"))


def test_test_paths_e_demos_do_wi_sao_isentos(repo):
    _write(repo, "tests/DEBUG_REPORT.md", "relatório dentro de tests/\n")
    _write(repo, "demos/wi_x/output.log", "log do demo do work item\n")

    pruned, kept = _prune_disposable_artifacts(
        repo, expected_files=["src/x.py"], work_item_id="wi_x"
    )

    assert pruned == []
    assert kept == []  # nenhum dos dois conta como "fora do plano"
    assert os.path.exists(os.path.join(repo, "tests/DEBUG_REPORT.md"))
    assert os.path.exists(os.path.join(repo, "demos/wi_x/output.log"))


def test_demo_de_outro_wi_nao_e_isento(repo):
    """`demos/<outro-wi>/` NÃO é o demo deste work item — um .log ali é lixo."""
    _write(repo, "demos/wi_outro/junk.log", "log de outro wi\n")

    pruned, _kept = _prune_disposable_artifacts(
        repo, expected_files=["src/x.py"], work_item_id="wi_x"
    )

    assert pruned == ["demos/wi_outro/junk.log"]


def test_arquivo_rastreado_modificado_fora_do_plano_nunca_e_tocado(repo):
    """Só arquivos NOVOS (untracked, `??`) entram no prune. Um arquivo EXISTENTE
    modificado fora do plano fica — é o L1/orçamento que o julga."""
    with open(os.path.join(repo, "app.js"), "w") as fh:
        fh.write("// modificado fora do plano\n")
    _write(repo, "trash.log", "lixo\n")

    pruned, kept = _prune_disposable_artifacts(
        repo, expected_files=["src/other.js"], work_item_id="wi_x"
    )

    assert pruned == ["trash.log"]
    assert kept == []  # app.js é rastreado → nem podado nem contado
    assert os.path.exists(os.path.join(repo, "app.js"))
    with open(os.path.join(repo, "app.js")) as fh:
        assert fh.read() == "// modificado fora do plano\n"  # a modificação fica
    porcelain = _git(repo, "status", "--porcelain")
    assert "app.js" in porcelain and "trash.log" not in porcelain


def test_git_indisponivel_nao_apaga_nada(tmp_path):
    """Best-effort: sem repo git, o prune não quebra e não apaga nada (o L1 é o
    gate duro)."""
    ws = str(tmp_path / "not-a-repo")
    os.makedirs(ws)
    _write(ws, "BUG_FIX_REPORT.md", "x\n")

    pruned, kept = _prune_disposable_artifacts(ws, expected_files=["a.py"], work_item_id="wi_x")

    assert pruned == []
    assert kept == []
    assert os.path.exists(os.path.join(ws, "BUG_FIX_REPORT.md"))


# ---------------------------------------------------------------------------
# Coder NÃO é dono dos testes (achado do disparo real na issue #1)
# ---------------------------------------------------------------------------
from sandbox_runtime.activities import _revert_coder_test_edits


@pytest.fixture()
def repo_with_test(tmp_path):
    ws = str(tmp_path / "ws2")
    os.makedirs(ws)
    _git(ws, "init", "-q", "-b", "main")
    _git(ws, "config", "user.email", "t@dse.local")
    _git(ws, "config", "user.name", "t")
    os.makedirs(os.path.join(ws, "test"))
    # teste EXISTENTE com um seed compartilhado (como o test/api.test.js do wallet)
    with open(os.path.join(ws, "test", "api.test.js"), "w") as fh:
        fh.write("// seed: Mercado 2026-07-10\nassert(first === 'Mercado');\n")
    with open(os.path.join(ws, "src.js"), "w") as fh:
        fh.write("// base\n")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", "base")
    return ws


def test_coder_edit_em_teste_existente_e_revertido(repo_with_test):
    """O bug da issue #1: o Coder mudou o seed compartilhado de test/api.test.js
    e quebrou um teste irmão. A edição do Coder em teste é revertida ao início
    do turno (o Tester é dono dos testes)."""
    ws = repo_with_test
    start = _git(ws, "rev-parse", "HEAD").strip()
    # Coder edita o teste (muda o seed) + edita a fonte
    with open(os.path.join(ws, "test", "api.test.js"), "w") as fh:
        fh.write("// seed MUDADO: Mercado 2026-06-10\nassert(first === 'Mercado');\n")
    with open(os.path.join(ws, "src.js"), "w") as fh:
        fh.write("// fix real\n")

    reverted = _revert_coder_test_edits(ws, start)

    assert reverted == ["test/api.test.js"]
    # teste voltou ao original (seed de julho)
    assert "2026-07-10" in open(os.path.join(ws, "test", "api.test.js")).read()
    # a fonte NÃO foi tocada pelo revert (só testes)
    assert "fix real" in open(os.path.join(ws, "src.js")).read()


def test_coder_cria_teste_novo_e_removido(repo_with_test):
    ws = repo_with_test
    start = _git(ws, "rev-parse", "HEAD").strip()
    _write(ws, "test/coder-invented.test.js", "// teste que o Coder inventou\n")
    reverted = _revert_coder_test_edits(ws, start)
    assert reverted == ["test/coder-invented.test.js"]
    assert not os.path.exists(os.path.join(ws, "test", "coder-invented.test.js"))


def test_revert_nao_toca_em_fonte(repo_with_test):
    ws = repo_with_test
    start = _git(ws, "rev-parse", "HEAD").strip()
    _write(ws, "src/novo.js", "export const x = 1;\n")
    with open(os.path.join(ws, "src.js"), "w") as fh:
        fh.write("// mudou\n")
    reverted = _revert_coder_test_edits(ws, start)
    assert reverted == []  # nenhum teste tocado
    assert os.path.exists(os.path.join(ws, "src", "novo.js"))
    assert "mudou" in open(os.path.join(ws, "src.js")).read()
