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
