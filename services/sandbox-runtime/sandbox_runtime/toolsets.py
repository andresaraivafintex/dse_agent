"""Toolsets stage-scoped (WSC-E3-T3/T4/T5).

Cada sessão de agente da Fase 2 recebe um toolset que é a ÚNICA superfície de
ferramentas disponível ao substrato. O enforcement é por allowlist explícita +
guarda de caminho — não por "boa vontade" do prompt:

- **PlannerToolset (read-only)**: só ferramentas de leitura. Qualquer tentativa
  de escrever arquivo, rodar teste que muta estado, ou tocar git FALHA com
  `ToolPermissionError` (teste de conformidade WSC-E3-T3).
- **TesterToolset**: leitura + `run_tests` + `write_file` SÓ em caminhos de
  teste; escrever fora de test path FALHA (WSC-E3-T4). Sem git (o commit dos
  testes é feito por código determinístico na Activity, escopado a test paths).
- **ReviewerToolset**: contexto fresco — só `read_plan`/`read_diff`. Sem acesso
  a repo, sem histórico do Coder, sem git (P3, WSC-E3-T5).

O substrato (`ScriptedAgentSession` em sessions.py, e em produção o adapter
OpenHands com o registro de ferramentas filtrado por este allowlist) nunca
executa uma tool sem passar por `Toolset.check` antes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Caminhos considerados "de teste" (TesterToolset só escreve aqui). Cobre os
# layouts comuns: pytest (`tests/`, `test_*.py`, `*_test.py`, `conftest.py`),
# jest/vitest (`*.test.ts`, `*.spec.ts`, `__tests__/`), go (`*_test.go`).
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


class ToolPermissionError(Exception):
    """Ferramenta não permitida para o stage atual — falha limpa (P6)."""


@dataclass(frozen=True)
class ToolInvocation:
    tool: str
    args: dict


class Toolset:
    """Base. `name`, allowlist e `check(invocation)` que levanta se negado."""

    name = "base"
    allowed: frozenset[str] = frozenset()

    def check(self, inv: ToolInvocation) -> None:
        if inv.tool not in self.allowed:
            raise ToolPermissionError(
                f"toolset '{self.name}': ferramenta '{inv.tool}' não permitida "
                f"(permitidas: {sorted(self.allowed)})"
            )

    def permits(self, tool: str) -> bool:
        try:
            self.check(ToolInvocation(tool=tool, args={}))
            return True
        except ToolPermissionError:
            return False


class PlannerToolset(Toolset):
    name = "planner"
    # SÓ leitura — nenhuma escrita, nenhum git, nenhum run que mute estado.
    allowed = frozenset(
        {"read_file", "search_code", "repo_map", "read_ticket", "read_agents_md", "read_codeowners", "list_skills"}
    )


class TesterToolset(Toolset):
    name = "tester"
    _READ = frozenset({"read_file", "search_code", "repo_map", "read_ticket"})
    _EXEC = frozenset({"run_tests"})
    _WRITE = frozenset({"write_file"})
    allowed = _READ | _EXEC | _WRITE

    def check(self, inv: ToolInvocation) -> None:
        if inv.tool in self._READ or inv.tool in self._EXEC:
            return
        if inv.tool in self._WRITE:
            path = inv.args.get("path", "")
            if not is_test_path(path):
                raise ToolPermissionError(
                    f"toolset 'tester': write_file só é permitido em caminhos de teste; "
                    f"'{path}' não é um. Edits de código de produção são do Coder, não do Tester."
                )
            return
        raise ToolPermissionError(
            f"toolset 'tester': ferramenta '{inv.tool}' não permitida "
            f"(sem git/PR — o commit dos testes é determinístico na Activity)."
        )


class ReviewerToolset(Toolset):
    name = "reviewer"
    # Contexto fresco: SÓ o plano + o diff. Sem repo, sem histórico do Coder,
    # sem git. P3 por construção (WSC-E3-T5).
    allowed = frozenset({"read_plan", "read_diff"})
