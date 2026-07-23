"""Classificação determinística de caminhos (P1) — compartilhada.

`is_test_path` era do sandbox-runtime (TesterToolset); promovida ao contrato
porque o plan_compliance do L1 também precisa dela: o Tester escreve testes POR
DESIGN em paths de teste, e o plano (Planner) nunca os lista — arquivos de
teste não podem contar como "fora do plano" (achado do disparo real 2026-07-22:
o L1 reprovaria toda tarefa com teste novo).
"""
from __future__ import annotations

import re

# Cobre os layouts comuns: pytest (`tests/`, `test_*.py`, `*_test.py`,
# `conftest.py`), jest/vitest/node:test (`*.test.ts`, `*.spec.ts`,
# `__tests__/`, `test/`), go (`*_test.go`).
_TEST_PATH_RES = [
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)__tests__/"),
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"_test\.py$"),
    re.compile(r"(^|/)conftest\.py$"),
    re.compile(r"\.(test|spec)\.[jt]sx?$"),
    re.compile(r"_test\.go$"),
]


def is_test_path(path: str) -> bool:
    p = path.replace("\\", "/")
    return any(rx.search(p) for rx in _TEST_PATH_RES)


# --- Artefatos DESCARTÁVEIS do runtime/CLI (prune pós-Coder) ----------------
# Desde que `expected_files` virou ADVISORY no L1 (o Planner adivinha os
# arquivos pelo TEXTO da issue, ANTES de ler o código — decisão do operador,
# ver a memória l1-expected-files-advisory), o prune pós-Coder NÃO pode mais
# apagar "tudo que está fora do plano": um arquivo-fonte NOVO e legítimo que o
# fix precisou criar ficaria fora de `expected_files` e sumiria silenciosamente
# antes do commit. `is_disposable_artifact` restringe o prune a LIXO óbvio
# (log/scratch/backup e o relatório espontâneo que o CLI escreve sobre o próprio
# trabalho, ex.: BUG_FIX_REPORT.md).
#
# INVARIANTE (coberta em packages/contracts/tests/test_paths.py): NUNCA
# classifica um arquivo-fonte como descartável. A assimetria é deliberada — na
# dúvida, MANTÉM: um lixo que escapa fica no diff (pego pelo orçamento de linhas
# do L1 / revisão humana); uma fonte apagada por engano quebra o fix sem deixar
# rastro no diff.

# Extensões que são lixo de runtime por definição (jamais código-fonte).
_DISPOSABLE_EXTS = frozenset({
    ".log", ".tmp", ".temp", ".bak", ".orig", ".rej",
    ".swp", ".swo", ".pyc", ".pyo", ".pid",
})

# Basenames exatos de lixo de SO/editor/processo.
_DISPOSABLE_BASENAMES = frozenset({
    ".ds_store", "thumbs.db", "desktop.ini", "nohup.out",
})

# SÓ arquivos de doc/texto podem ser podados pela convenção de NOME de relatório
# (um .py/.js/.ts nunca entra por esta regra — é o que blinda a fonte).
_REPORT_DOC_EXTS = frozenset({".md", ".markdown", ".txt", ".rst"})

# Palavras que denunciam um relatório espontâneo do CLI (BUG_FIX_REPORT.md,
# IMPLEMENTATION_SUMMARY.md, CHANGES_WALKTHROUGH.txt…). Curada de propósito:
# NÃO inclui README/CHANGELOG/CONTRIBUTING/LICENSE/requirements — documentos e
# manifestos legítimos e mantidos, que devem sobreviver.
_REPORT_NAME_KEYWORDS = ("REPORT", "SUMMARY", "WALKTHROUGH", "FINDINGS", "VERIFICATION")


def is_disposable_artifact(path: str) -> bool:
    """True se `path` é um artefato DESCARTÁVEL óbvio do runtime/CLI — log,
    scratch, backup, lixo de SO/editor, ou um relatório espontâneo que o CLI
    escreve sobre o próprio trabalho (BUG_FIX_REPORT.md).

    Usado pelo prune pós-Coder para apagar SÓ lixo. Ver a nota acima sobre a
    invariante anti-fonte e a assimetria de manter-em-dúvida — em particular,
    a heurística de nome-de-relatório só se aplica a extensões de doc/texto,
    então nenhum arquivo-fonte (independente do nome) é jamais descartável.
    """
    p = path.replace("\\", "/")
    base = p.rsplit("/", 1)[-1]
    if base.lower() in _DISPOSABLE_BASENAMES:
        return True
    stem, dot, ext = base.rpartition(".")
    if not dot:  # sem extensão (Makefile, LICENSE, Dockerfile…) — nunca lixo
        return False
    ext = "." + ext.lower()
    if ext in _DISPOSABLE_EXTS:
        return True
    if ext in _REPORT_DOC_EXTS:
        up = stem.upper()
        return any(kw in up for kw in _REPORT_NAME_KEYWORDS)
    return False


# Lockfile -> manifesto que o declara. Rodar o gerenciador de pacotes (npm test,
# poetry run…) reescreve metadados do lockfile SEM mudança de dependência
# (achado do disparo real 2026-07-22: npm mudou 16 linhas de package-lock.json
# e o diff_budget reprovou a tarefa como "mudança fora do plano"). Churn de
# lockfile só é mudança REAL quando o manifesto par também mudou.
LOCKFILE_MANIFESTS: dict[str, str] = {
    "package-lock.json": "package.json",
    "npm-shrinkwrap.json": "package.json",
    "yarn.lock": "package.json",
    "pnpm-lock.yaml": "package.json",
    "bun.lockb": "package.json",
    "poetry.lock": "pyproject.toml",
    "uv.lock": "pyproject.toml",
    "Pipfile.lock": "Pipfile",
    "Cargo.lock": "Cargo.toml",
    "Gemfile.lock": "Gemfile",
    "composer.lock": "composer.json",
    "go.sum": "go.mod",
}


def lockfile_manifest_for(path: str) -> str | None:
    """Se `path` é um lockfile conhecido, retorna o path do manifesto par
    (mesmo diretório); senão None."""
    p = path.replace("\\", "/")
    manifest = LOCKFILE_MANIFESTS.get(p.rsplit("/", 1)[-1])
    if manifest is None:
        return None
    head, _, _ = p.rpartition("/")
    return f"{head}/{manifest}" if head else manifest


def is_lockfile_churn(path: str, files_changed: list[str] | set[str]) -> bool:
    """True quando `path` é lockfile e o manifesto par NÃO está no diff —
    churn mecânico do gerenciador de pacotes, não uma mudança declarável."""
    manifest = lockfile_manifest_for(path)
    if manifest is None:
        return False
    changed = {f.replace("\\", "/") for f in files_changed}
    return manifest not in changed
