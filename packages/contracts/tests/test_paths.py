"""Classificação de caminhos descartáveis (`is_disposable_artifact`).

Reconciliação de 2026-07-22: como `expected_files` virou advisory no L1 (ver a
decisão do operador), o prune pós-Coder passou a apagar SÓ lixo óbvio do CLI, em
vez de "tudo fora do plano". A invariante central — que blinda a correção — é
que NENHUM arquivo-fonte é jamais classificado como descartável.
"""
from __future__ import annotations

import pytest

from dse_contracts.paths import is_disposable_artifact


# Os dois casos do enunciado, explícitos.
def test_relatorio_espontaneo_do_cli_e_descartavel():
    assert is_disposable_artifact("BUG_FIX_REPORT.md") is True


def test_arquivo_fonte_novo_legitimo_nao_e_descartavel():
    assert is_disposable_artifact("src/novo-modulo.js") is False


# --- INVARIANTE anti-fonte: nenhuma extensão de código é descartável, nem
# quando o NOME parece relatório (report.py, summary.js…). É isto que garante
# que o prune nunca apaga a correção. ---------------------------------------
_SOURCE_EXTS = [
    "py", "js", "jsx", "ts", "tsx", "mjs", "cjs", "go", "rs", "java", "rb",
    "php", "c", "cc", "cpp", "h", "hpp", "cs", "kt", "swift", "scala", "sql",
    "css", "scss", "html", "vue", "svelte", "sh", "yaml", "yml", "json",
    "toml", "xml", "proto", "gradle", "lua", "dart", "ex", "exs", "clj",
]


@pytest.mark.parametrize("ext", _SOURCE_EXTS)
def test_nenhuma_extensao_de_fonte_e_descartavel(ext):
    assert is_disposable_artifact(f"src/modulo.{ext}") is False
    # Nem com nome de relatório — a heurística de nome só vale p/ doc/texto.
    assert is_disposable_artifact(f"REPORT.{ext}") is False
    assert is_disposable_artifact(f"pkg/IMPLEMENTATION_SUMMARY.{ext}") is False


@pytest.mark.parametrize(
    "path",
    [
        "build.log",
        "server.tmp",
        "scratch.temp",
        "config.py.bak",
        "patch.orig",
        "merge.rej",
        ".file.swp",
        "module.pyc",
        "daemon.pid",
    ],
)
def test_extensoes_de_runtime_sao_descartaveis(path):
    assert is_disposable_artifact(path) is True


@pytest.mark.parametrize("base", [".DS_Store", "Thumbs.db", "desktop.ini", "nohup.out"])
def test_lixo_de_so_editor_e_descartavel(base):
    assert is_disposable_artifact(base) is True
    assert is_disposable_artifact(f"sub/dir/{base}") is True


@pytest.mark.parametrize(
    "path",
    [
        "BUG_FIX_REPORT.md",
        "IMPLEMENTATION_SUMMARY.md",
        "docs/CHANGES_WALKTHROUGH.txt",
        "findings.md",  # case-insensitive: FINDINGS
        "sub/verification-notes.rst",  # VERIFICATION
        "SUMMARY.markdown",
    ],
)
def test_relatorios_de_doc_sao_descartaveis(path):
    assert is_disposable_artifact(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "docs/architecture.md",
        "docs/api.md",
        "requirements.txt",  # NUNCA — é manifesto de dependências
        "notes/getting-started.md",
        "LICENSE",
        "Makefile",
        "Dockerfile",
        ".gitignore",
        ".env",
    ],
)
def test_docs_e_arquivos_legitimos_sobrevivem(path):
    assert is_disposable_artifact(path) is False


def test_normaliza_separador_windows():
    assert is_disposable_artifact("sub\\dir\\BUG_FIX_REPORT.md") is True
    assert is_disposable_artifact("sub\\dir\\novo-modulo.js") is False
