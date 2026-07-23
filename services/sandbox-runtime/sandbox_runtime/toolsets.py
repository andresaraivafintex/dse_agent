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
  Fase 3 (WSC-E3-T4b, adendo 02): `demos/<work_item_id>/` é um path de escrita
  PERMITIDO adicional — é onde o Tester autora o teste `@demo` Playwright que o
  pipeline de evidência do WS-E executa. A permissão é escopada ao work_item da
  sessão: `demos/<outro-id>/` continua BLOQUEADO (isolamento entre tarefas).
- **ReviewerToolset**: contexto fresco — só `read_plan`/`read_diff`. Sem acesso
  a repo, sem histórico do Coder, sem git (P3, WSC-E3-T5).

O substrato (`ScriptedAgentSession` em sessions.py, e em produção o adapter
OpenHands com o registro de ferramentas filtrado por este allowlist) nunca
executa uma tool sem passar por `Toolset.check` antes.
"""
from __future__ import annotations

from dataclasses import dataclass

# Caminhos considerados "de teste" (TesterToolset só escreve aqui). Cobre os
# layouts comuns: pytest (`tests/`, `test_*.py`, `*_test.py`, `conftest.py`),
# jest/vitest (`*.test.ts`, `*.spec.ts`, `__tests__/`), go (`*_test.go`).
# Promovido ao contrato compartilhado (o plan_compliance do L1 também usa —
# ver dse_contracts.paths). Re-export mantém os imports existentes.
from dse_contracts.paths import _TEST_PATH_RES, is_test_path  # noqa: F401


def demo_dir_for(work_item_id: str) -> str:
    """Convenção ADR-27/WSC-E3-T4b: diretório canônico do teste `@demo` de uma
    tarefa. O default de `RunDemoEvidenceInput.demo_dir` (contrato da fundação)
    deriva daqui — WS-E executa `npx playwright test --grep @demo` neste path."""
    return f"demos/{work_item_id}/"


def is_demo_path(path: str, work_item_id: str) -> bool:
    """True se `path` está DENTRO de `demos/<work_item_id>/` (deste work item —
    nunca de outro). Recusa `..` para não permitir escapar do prefixo."""
    if not work_item_id:
        return False
    p = path.replace("\\", "/").lstrip("/")
    if ".." in p.split("/"):
        return False
    return p.startswith(demo_dir_for(work_item_id))


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
    """Fase 2: write só em test paths. Fase 3 (WSC-E3-T4b): quando construído
    com `work_item_id`, `demos/<work_item_id>/` vira um path de escrita
    permitido ADICIONAL (convenção do teste `@demo` Playwright, ADR-27) —
    escopado ao work item da sessão; `demos/` de outro work item continua
    bloqueado. Construtor sem argumento preserva o comportamento da Fase 2
    (nenhum write em `demos/`)."""

    name = "tester"
    _READ = frozenset({"read_file", "search_code", "repo_map", "read_ticket"})
    _EXEC = frozenset({"run_tests"})
    _WRITE = frozenset({"write_file"})
    allowed = _READ | _EXEC | _WRITE

    def __init__(self, work_item_id: str = ""):
        self.work_item_id = work_item_id

    def check(self, inv: ToolInvocation) -> None:
        if inv.tool in self._READ or inv.tool in self._EXEC:
            return
        if inv.tool in self._WRITE:
            path = inv.args.get("path", "")
            p = path.replace("\\", "/").lstrip("/")
            if p.startswith("demos/") or p == "demos":
                # Namespace de EVIDÊNCIA (Fase 3): escopado por work item. A
                # regra genérica de test path (ex.: `*.spec.js` em qualquer
                # lugar) NÃO vale aqui dentro — senão o Tester de uma tarefa
                # escreveria no demo de outra só nomeando `*.spec.js`.
                if is_demo_path(path, self.work_item_id):
                    return
                raise ToolPermissionError(
                    f"toolset 'tester': dentro de 'demos/' a escrita é permitida SÓ em "
                    f"'{demo_dir_for(self.work_item_id) if self.work_item_id else 'demos/<work_item_id>/ (sessão sem work item: nenhuma)'}'; "
                    f"'{path}' está fora desse escopo."
                )
            if is_test_path(path):
                return
            raise ToolPermissionError(
                f"toolset 'tester': write_file só é permitido em caminhos de teste "
                f"ou em '{demo_dir_for(self.work_item_id) if self.work_item_id else 'demos/<work_item_id>/'}'; "
                f"'{path}' não é nenhum dos dois. Edits de código de produção são do Coder, não do Tester."
            )
        raise ToolPermissionError(
            f"toolset 'tester': ferramenta '{inv.tool}' não permitida "
            f"(sem git/PR — o commit dos testes é determinístico na Activity)."
        )


class ReviewerToolset(Toolset):
    name = "reviewer"
    # Contexto fresco: SÓ o plano + o diff. Sem repo, sem histórico do Coder,
    # sem git. P3 por construção (WSC-E3-T5).
    allowed = frozenset({"read_plan", "read_diff"})
