"""Sessões stage-scoped da Fase 2 (WSC-E3-T3/T4/T5): hidratação de contexto do
Planner, classificador de risco DETERMINÍSTICO, runner de sessão roteirizado
(que executa ferramentas SEMPRE através do toolset), e a sessão Reviewer de
contexto fresco.

P1 (nenhuma decisão de fluxo por LLM): o `risk_class` do PlanArtifact — que
dirige o gate de aprovação do WS-B — é derivado por `classify_risk_class`
(código determinístico sobre o blast radius declarado), NÃO pela palavra do
LLM. A sessão Planner (LLM) propõe steps/expected_files/test_plan; a
classificação de risco é um piso determinístico que o LLM não consegue
rebaixar. Assim o gate a jusante nunca depende de um julgamento de modelo.

P3 (nenhum produtor aprova o próprio trabalho): `FreshReviewerSession` é
construída SÓ a partir de `PlanArtifact` + diff — não há parâmetro, atributo
ou canal que carregue o histórico do Coder para dentro dela (provado por
construção em `tests/test_reviewer_fresh_context.py`).
"""
from __future__ import annotations

import fnmatch
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dse_contracts import L2Verdict, PlanArtifact

from .retrieval import RetrievalHit, RetrievalService, render_untrusted_context
from .skill_registry import Skill, read_approved_skills
from .toolsets import ReviewerToolset, TesterToolset, Toolset, ToolInvocation


# ---------------------------------------------------------------------------
# Classificador de risco determinístico (piso de risco — P1)
# ---------------------------------------------------------------------------
# Globs cujo toque eleva o risco. Sensível = requer aprovação humana de plano
# no WS-B (gate de risk class). Estes são padrões conservadores do domínio
# fintech regulado; um access bundle por tenant (WS-F) pode estendê-los.
_HIGH_RISK_GLOBS = [
    ".github/workflows/*",
    "**/migrations/*",
    "migrations/*",
    "**/*auth*",
    "**/*payment*",
    "**/*billing*",
    "**/secrets*",
    "infra/*",
    "**/Dockerfile*",
]
_MEDIUM_RISK_GLOBS = [
    "**/*.sql",
    "**/config*",
    "**/settings*",
    "pyproject.toml",
    "requirements*.txt",
    "package.json",
]


def _matches_any(path: str, globs: list[str]) -> bool:
    p = path.replace("\\", "/")
    return any(fnmatch.fnmatch(p, g) or fnmatch.fnmatch(p, g.lstrip("*/")) for g in globs)


def classify_risk_class(
    expected_files: list[str],
    diff_budget_lines: int,
    forbidden_paths: list[str] | None = None,
) -> str:
    """Deriva risk_class ('low'|'medium'|'high') DETERMINISTICAMENTE do blast
    radius declarado. Regras (piso — o gate do WS-B decide o que fazer com
    cada nível):
      - high  : toca qualquer forbidden_path OU qualquer _HIGH_RISK_GLOB, OU
                diff_budget > 800 linhas;
      - medium: toca qualquer _MEDIUM_RISK_GLOB OU diff_budget > 300 linhas OU
                > 15 arquivos;
      - low   : caso contrário.
    """
    forbidden = forbidden_paths or []
    for f in expected_files:
        fp = f.replace("\\", "/")
        if any(fp.startswith(fb.rstrip("*")) or fnmatch.fnmatch(fp, fb + "*") for fb in forbidden):
            return "high"
        if _matches_any(fp, _HIGH_RISK_GLOBS):
            return "high"
    if diff_budget_lines > 800:
        return "high"
    if diff_budget_lines > 300 or len(expected_files) > 15:
        return "medium"
    for f in expected_files:
        if _matches_any(f, _MEDIUM_RISK_GLOBS):
            return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Hidratação de contexto do Planner (WSC-E3-T3)
# ---------------------------------------------------------------------------
@dataclass
class PlannerContext:
    """Bundle de contexto do Planner. `skills`/`agents_md`/`codeowners` são
    CONFIÁVEIS (curados/versionados no repo do tenant). `retrieval_hits` e
    `tickets` são NÃO CONFIÁVEIS (input externo/de código) e são renderizados
    num bloco marcado por `render_untrusted_context`."""

    work_item_id: str
    tenant_id: str
    agents_md: str = ""
    codeowners: str = ""
    skills: list[Skill] = field(default_factory=list)
    tickets: list[str] = field(default_factory=list)
    retrieval_hits: list[RetrievalHit] = field(default_factory=list)
    repo_map: str = ""

    def render(self) -> str:
        parts: list[str] = [f"# Contexto do Planner — work_item {self.work_item_id} (tenant {self.tenant_id})"]
        if self.agents_md:
            parts.append("## AGENTS.md (confiável)\n" + self.agents_md)
        if self.codeowners:
            parts.append("## CODEOWNERS (confiável)\n" + self.codeowners)
        if self.skills:
            parts.append("## Skills aprovadas do tenant (confiável)\n" + "\n\n".join(s.as_context_block() for s in self.skills))
        if self.repo_map:
            parts.append("## Repo map\n" + self.repo_map)
        # Não confiável por último, claramente demarcado.
        untrusted_blocks: list[str] = []
        if self.tickets:
            untrusted_blocks.append("Tickets relacionados:\n" + "\n---\n".join(self.tickets))
        if self.retrieval_hits:
            untrusted_blocks.append(render_untrusted_context(self.retrieval_hits))
        if untrusted_blocks:
            parts.append("\n".join(untrusted_blocks))
        return "\n\n".join(parts)


def _read_repo_file(workspace_dir: str, rel: str) -> str:
    p = Path(workspace_dir) / rel
    try:
        return p.read_text()
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return ""


def hydrate_planner_context(
    *,
    work_item_id: str,
    tenant_id: str,
    workspace_dir: str,
    repo: str,
    instruction: str,
    task_class: str = "default",
    related_tickets: list[str] | None = None,
    retrieval: RetrievalService | None = None,
    skills_conn=None,
) -> PlannerContext:
    """Monta o `PlannerContext`: AGENTS.md + CODEOWNERS (do workspace), skills
    aprovadas do tenant (skill_registry, E4), tickets relacionados, e os
    top-k trechos do índice de retrieval (E5) relevantes à instrução. O
    Planner é read-only — nada aqui muta o repo."""
    agents_md = _read_repo_file(workspace_dir, "AGENTS.md")
    codeowners = _read_repo_file(workspace_dir, "CODEOWNERS") or _read_repo_file(workspace_dir, ".github/CODEOWNERS")
    skills = read_approved_skills(tenant_id, task_class=task_class, conn=skills_conn)

    hits: list[RetrievalHit] = []
    repo_map = ""
    if retrieval is not None:
        hits = retrieval.search(tenant_id, instruction, k=5, repo=repo)
        repo_map = retrieval.repo_map(tenant_id, repo)

    return PlannerContext(
        work_item_id=work_item_id,
        tenant_id=tenant_id,
        agents_md=agents_md,
        codeowners=codeowners,
        skills=skills,
        tickets=list(related_tickets or []),
        retrieval_hits=hits,
        repo_map=repo_map,
    )


# ---------------------------------------------------------------------------
# Runner de sessão roteirizado — executa toda tool via o toolset (P0 de teste)
# ---------------------------------------------------------------------------
@dataclass
class ToolResult:
    tool: str
    ok: bool
    detail: Any = None


class ScriptedAgentSession:
    """Substrato roteirizado para Planner/Tester (análogo ao FakeSubstrate do
    Coder): não chama LLM, executa uma lista de `ToolInvocation`. TODA
    invocação passa por `toolset.check` ANTES de despachar — é isto que torna
    o teste de conformação real (um write no Planner levanta
    `ToolPermissionError` de verdade, não por convenção).

    Em produção o adapter OpenHands registra só as ferramentas cujo nome está
    no allowlist do toolset e roteia cada tool-call pelo mesmo `check` — ver
    README (mesmo padrão do `OpenHandsSubstrate` do Coder na Fase 1).
    """

    def __init__(
        self,
        *,
        toolset: Toolset,
        workspace_dir: str,
        retrieval: RetrievalService | None = None,
        tenant_id: str = "",
        repo: str = "",
        context_reads: dict[str, str] | None = None,
    ):
        self.toolset = toolset
        self.workspace_dir = workspace_dir
        self._retrieval = retrieval
        self._tenant_id = tenant_id
        self._repo = repo
        self._context_reads = context_reads or {}
        self.log: list[ToolResult] = []

    def invoke(self, tool: str, **args: Any) -> ToolResult:
        inv = ToolInvocation(tool=tool, args=args)
        # 1) enforcement — levanta ToolPermissionError se negado.
        self.toolset.check(inv)
        # 2) despacho da tool permitida.
        detail = self._dispatch(inv)
        res = ToolResult(tool=tool, ok=True, detail=detail)
        self.log.append(res)
        return res

    def run_script(self, script: list[dict[str, Any]]) -> list[ToolResult]:
        results = []
        for step in script:
            tool = step["tool"]
            args = {k: v for k, v in step.items() if k != "tool"}
            results.append(self.invoke(tool, **args))
        return results

    def _dispatch(self, inv: ToolInvocation) -> Any:
        t, a = inv.tool, inv.args
        if t == "read_file":
            return _read_repo_file(self.workspace_dir, a["path"])
        if t == "write_file":
            p = Path(self.workspace_dir) / a["path"]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(a["content"])
            return str(p)
        if t == "search_code":
            if self._retrieval is None:
                return []
            return self._retrieval.search(self._tenant_id, a["query"], k=a.get("k", 5), repo=self._repo or None)
        if t == "repo_map":
            if self._retrieval is None:
                return ""
            return self._retrieval.repo_map(self._tenant_id, self._repo)
        if t == "run_tests":
            return self._run_tests(a.get("paths"))
        if t in ("read_ticket", "read_agents_md", "read_codeowners", "list_skills"):
            return self._context_reads.get(t, "")
        # read_plan/read_diff são servidos pela FreshReviewerSession, não aqui.
        return None

    def _run_tests(self, paths: list[str] | None) -> dict[str, Any]:
        """Executa os testes DE VERDADE dentro do workspace (WSC-E3-T4: os
        testes escritos rodam no L1, não só são gerados). Detecção
        DETERMINÍSTICA do runner (P1): repo Node (package.json com script
        `test`) → `npm test`; senão pytest. Devolve rc + stdout resumido."""
        import json as _json
        import os as _os

        pkg = _os.path.join(self.workspace_dir, "package.json")
        cmd: list[str]
        env = {**_os.environ, "CI": "1"}
        if _os.path.isfile(pkg):
            try:
                has_test = "test" in (_json.load(open(pkg)).get("scripts") or {})
            except Exception:  # noqa: BLE001 — package.json inválido => pytest
                has_test = False
            if has_test:
                # deps primeiro (clone fresh não tem node_modules); best-effort —
                # se falhar, o npm test abaixo reporta o erro de verdade.
                if not _os.path.isdir(_os.path.join(self.workspace_dir, "node_modules")):
                    subprocess.run(
                        ["npm", "install", "--no-audit", "--no-fund"],
                        cwd=self.workspace_dir, capture_output=True, text=True, timeout=600,
                    )
                cmd = ["npm", "test", "--silent"]
            else:
                cmd = [sys.executable, "-m", "pytest", "-q"]
        else:
            cmd = [sys.executable, "-m", "pytest", "-q"]
            if paths:
                cmd += paths
        try:
            proc = subprocess.run(
                cmd, cwd=self.workspace_dir, capture_output=True, text=True,
                timeout=600, env=env,
            )
            rc, out, err = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            rc, out, err = 124, (exc.stdout or ""), f"timeout 600s: {exc.cmd}"
        return {
            "returncode": rc,
            "passed": rc == 0,
            "command": " ".join(cmd),
            "stdout_tail": (out or "")[-4000:],
            "stderr_tail": (err or "")[-2000:],
        }


# ---------------------------------------------------------------------------
# Sessão Reviewer de contexto fresco (WSC-E3-T5) — P3 por construção
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ReviewerContext:
    """Contexto IMUTÁVEL e MÍNIMO da sessão L2: SÓ o plano + o diff final.

    Deliberadamente NÃO existe nenhum campo que carregue histórico do Coder,
    logs de turnos, thoughts, tool-calls ou qualquer transcrição da sessão
    produtora. Isto é o "por construção" de P3: a única forma de dar contexto
    ao Reviewer é por estes dois campos.
    """

    work_item_id: str
    plan: PlanArtifact
    diff: str

    def render(self) -> str:
        return (
            f"# Revisão L2 — work_item {self.work_item_id}\n\n"
            f"## Plano a que o diff deve aderir\n"
            f"steps: {self.plan.steps}\n"
            f"expected_files (blast radius declarado): {self.plan.expected_files}\n"
            f"diff_budget_lines: {self.plan.diff_budget_lines}\n"
            f"test_plan: {self.plan.test_plan}\n"
            f"risk_class: {self.plan.risk_class}\n"
            f"forbidden_paths: {self.plan.forbidden_paths}\n\n"
            f"## Diff final a revisar\n{self.diff}"
        )


class FreshReviewerSession:
    """Sessão NOVA (sem estado compartilhado com o Coder) que julga aderência
    do diff ao plano/convenções. Recebe SÓ `ReviewerContext` (plan+diff).

    O julgamento em si (produzir objeções) é do substrato — em produção uma
    conversa OpenHands fresca semeada só com `context.render()`. Aqui, para
    teste, `review()` aceita um `verdict_fn(context) -> (passed, objections)`
    determinístico (o "modelo" roteirizado), provando toda a plumbing sem
    inferência real. Nota P1/P3: o veredito L2 é uma RECOMENDAÇÃO que gateia a
    progressão — o merge continua sendo humano (nenhuma decisão de fluxo por
    LLM); e por ser sessão fresca, nunca é o produtor aprovando o próprio
    trabalho.
    """

    def __init__(self, context: ReviewerContext, toolset: ReviewerToolset | None = None):
        self.context = context
        self.toolset = toolset or ReviewerToolset()

    def read_plan(self) -> PlanArtifact:
        self.toolset.check(ToolInvocation(tool="read_plan", args={}))
        return self.context.plan

    def read_diff(self) -> str:
        self.toolset.check(ToolInvocation(tool="read_diff", args={}))
        return self.context.diff

    def review(self, verdict_fn) -> L2Verdict:
        passed, objections, cost = verdict_fn(self.context)
        return L2Verdict(
            work_item_id=self.context.work_item_id,
            passed=passed,
            objections=list(objections),
            cost_usd=float(cost),
        )
