"""Contrato de Activities entre WS-B (orquestrador, quem chama), WS-C (sandbox,
quem implementa as Activities de execução) e WS-E (validação/PR, quem
implementa as Activities de gate/finalize). Os tipos abaixo são o que
atravessa a fronteira Activity — os `@activity.defn` de verdade (decorados
com `temporalio.activity`) vivem no serviço dono, mas usam estes tipos como
parâmetro/retorno para que WS-B possa escrever o workflow contra uma
interface estável antes de qualquer implementação existir.

Convenção: cada activity real é registrada no Worker único de
`services/orchestrator/worker.py` (dono: WS-B), que importa o módulo de
Activities de cada workstream. Import defensivo (try/except ImportError) é
esperado enquanto os workstreams constroem em paralelo.
"""
from __future__ import annotations

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Nomes de activity (usados em `@activity.defn(name=...)` e em
# `workflow.execute_activity(name, ...)` — precisam bater nos dois lados).
# ---------------------------------------------------------------------------
ACTIVITY_PROVISION_SANDBOX = "provision_sandbox"
ACTIVITY_RUN_CODER_TURN = "run_coder_turn"
ACTIVITY_CHECKPOINT_SANDBOX = "checkpoint_sandbox"
ACTIVITY_REBUILD_SANDBOX = "rebuild_sandbox"
ACTIVITY_TEARDOWN_SANDBOX = "teardown_sandbox"
ACTIVITY_RUN_L1_PIPELINE = "run_l1_pipeline"
ACTIVITY_FINALIZE_PR = "finalize_pr"
ACTIVITY_POST_TRACKING_COMMENT = "post_tracking_comment"
ACTIVITY_CONSUME_CI_STATUS = "consume_ci_status"
ACTIVITY_EMIT_AUDIT = "emit_audit_event"


# ---------------------------------------------------------------------------
# Dono: WS-C (services/sandbox-runtime)
# ---------------------------------------------------------------------------
class SandboxHandle(BaseModel):
    sandbox_id: str
    work_item_id: str
    tenant_id: str
    branch: str
    container_id: str | None = None  # id do container Docker por trás do handle


class CheckpointRef(BaseModel):
    work_item_id: str
    git_ref: str  # commit sha no branch da tarefa
    phase: str  # nome da fronteira de fase em que o checkpoint foi tirado


class CoderTurnResult(BaseModel):
    sandbox_id: str
    diff_summary: str
    files_changed: list[str]
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0


# ---------------------------------------------------------------------------
# Dono: WS-E (services/validation)
# ---------------------------------------------------------------------------
class L1Finding(BaseModel):
    check: str  # "lint" | "typecheck" | "test" | "build" | "sast" | "secret_scan" | "diff_budget" | "forbidden_paths"
    passed: bool
    detail: str = ""


class L1Result(BaseModel):
    work_item_id: str
    passed: bool
    findings: list[L1Finding]


class PrRef(BaseModel):
    work_item_id: str
    pr_number: int
    url: str


class CiStatusResult(BaseModel):
    work_item_id: str
    pr_number: int
    status: str  # "pending" | "green" | "red"
