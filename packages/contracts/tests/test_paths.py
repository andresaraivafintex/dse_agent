"""Classification of disposable paths (`is_disposable_artifact`).

Reconciliation of 2026-07-22: since `expected_files` became advisory in L1 (see
the operator decision), the post-Coder prune now deletes ONLY obvious CLI
garbage, instead of "everything outside the plan". The central invariant — the
one that shields the fix — is that NO source file is ever classified as
disposable.
"""
from __future__ import annotations

import pytest

from dse_contracts.paths import is_disposable_artifact


# The two cases from the statement, spelled out.
def test_relatorio_espontaneo_do_cli_e_descartavel():
    assert is_disposable_artifact("BUG_FIX_REPORT.md") is True


def test_arquivo_fonte_novo_legitimo_nao_e_descartavel():
    assert is_disposable_artifact("src/novo-modulo.js") is False


# --- Anti-source INVARIANT: no code extension is disposable, not even when
# the NAME looks like a report (report.py, summary.js…). This is what
# guarantees the prune never deletes the fix. -------------------------------
_SOURCE_EXTS = [
    "py", "js", "jsx", "ts", "tsx", "mjs", "cjs", "go", "rs", "java", "rb",
    "php", "c", "cc", "cpp", "h", "hpp", "cs", "kt", "swift", "scala", "sql",
    "css", "scss", "html", "vue", "svelte", "sh", "yaml", "yml", "json",
    "toml", "xml", "proto", "gradle", "lua", "dart", "ex", "exs", "clj",
]


@pytest.mark.parametrize("ext", _SOURCE_EXTS)
def test_nenhuma_extensao_de_fonte_e_descartavel(ext):
    assert is_disposable_artifact(f"src/modulo.{ext}") is False
    # Not even with a report name — the name heuristic only applies to doc/text.
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
        "requirements.txt",  # NEVER — it is a dependency manifest
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
