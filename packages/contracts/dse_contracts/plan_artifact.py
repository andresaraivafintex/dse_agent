"""PlanArtifact — na Fase 2 é produzido por uma sessão Planner read-only
dedicada (WSC-E3-T3). Na Fase 1 (Coder único, sem Planner separado) o Coder
preenche uma versão mínima deste artefato *antes* de escrever qualquer diff,
porque o diff-budget/forbidden-paths enforcement do L1 (WSE-E1-T3) depende
dele existir independentemente de quem o produziu.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class PlanArtifact(BaseModel):
    work_item_id: str
    steps: list[str] = Field(default_factory=list)
    expected_files: list[str] = Field(default_factory=list)  # blast radius declarado
    # Escape hatch explicito para tarefas que deliberadamente nao produzem
    # patch. Um plano vazio sem esta marca e invalido no workflow; manter o
    # default False torna payloads historicos aditivos e seguros.
    no_code_change: bool = False
    diff_budget_lines: int = 400  # default conservador; access bundle pode ajustar (Fase 2)
    test_plan: str = ""
    risk_class: str = "low"  # Fase 1: informativo apenas — gate de aprovação é Fase 2
    forbidden_paths: list[str] = Field(
        default_factory=lambda: [".github/workflows/", "migrations/"]
    )
