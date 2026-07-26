"""PlanArtifact — in Phase 2 it is produced by a dedicated read-only Planner
session (WSC-E3-T3). In Phase 1 (single Coder, no separate Planner) the Coder
fills in a minimal version of this artifact *before* writing any diff, because
the L1 diff-budget/forbidden-paths enforcement (WSE-E1-T3) depends on it
existing regardless of who produced it.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class PlanArtifact(BaseModel):
    work_item_id: str
    steps: list[str] = Field(default_factory=list)
    expected_files: list[str] = Field(default_factory=list)  # declared blast radius
    # Explicit escape hatch for tasks that deliberately produce no patch. An
    # empty plan without this flag is invalid in the workflow; keeping the
    # default False makes historical payloads additive and safe.
    no_code_change: bool = False
    diff_budget_lines: int = 400  # conservative default; access bundle may adjust (Phase 2)
    test_plan: str = ""
    risk_class: str = "low"  # Phase 1: informational only — the approval gate is Phase 2
    forbidden_paths: list[str] = Field(
        default_factory=lambda: [".github/workflows/", "migrations/"]
    )
