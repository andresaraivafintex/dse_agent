"""Tipos que atravessam a fronteira do workflow (`@workflow.run` input/output e
`continue_as_new`). Dataclasses simples (nao pydantic) de proposito: o
workflow roda dentro do sandbox deterministico do Temporal Python SDK e
dataclasses com tipos primitivos sao o caminho mais bem suportado pelo data
converter default sem plugins extras. Tipos pydantic ricos (WorkItem,
PlanArtifact, os tipos de `dse_contracts.activities`) sao usados livremente
nos *parametros/retorno de Activity*, que rodam fora do sandbox.
"""
from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Fases grosseiras do workflow — cada uma fecha com `continue_as_new` para
# manter o historico de eventos pequeno (WSB-E2-T1).
# ---------------------------------------------------------------------------
PHASE_INTAKE = "intake"
PHASE_IMPLEMENTATION = "implementation"
PHASE_REVIEW = "review"
PHASE_TERMINAL = "terminal"


@dataclass
class WorkItemLifecycleInput:
    """Estado que atravessa `continue_as_new` entre fases. `status` usa os
    valores (strings) de `dse_contracts.work_item.WorkItemStatus`."""

    work_item_id: str
    tenant_id: str
    requester: str
    repo: str | None = None
    base_branch: str | None = None
    data_class: str = "internal"
    acceptance_criteria: str | None = None

    phase: str = PHASE_INTAKE
    status: str = "new"

    clarification_rounds: int = 0
    coder_retry_count: int = 0
    review_round: int = 0

    sandbox_id: str | None = None
    branch: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None

    # payload textual acumulado de respostas de clarificacao (para o Coder
    # eventualmente consumir via Activity — Fase 1 nao interpreta com LLM
    # dentro do workflow, so repassa; P1: nenhuma decisao de fluxo por LLM).
    clarification_notes: list[str] = field(default_factory=list)

    terminal_detail: str | None = None

    # ------------------------------------------------------------------
    # Fase 2 — split de sessoes + gate de aprovacao de plano (WSB-E2-T3
    # estendida / WSB-E3-T2/T3). Sobrevivem a `continue_as_new` como o resto
    # do estado deterministico do workflow.
    # ------------------------------------------------------------------
    # PlanArtifact serializado (produzido pelo Planner read-only ANTES do
    # gate). Passado para o Reviewer L2 junto do diff final — e SO isto +
    # diff, nunca o historico do Coder (P3).
    plan_json: dict = field(default_factory=dict)
    # classe de risco EFETIVA (planner + classificacao deterministica de
    # defesa-em-profundidade — ver policy.classify_risk). Dirige o gate.
    risk_class: str = "low"
    # aprovadores resolvidos pela cascata CODEOWNERS -> access bundle (WS-F),
    # preenchido quando o gate estaciona em awaiting_plan_approval.
    approvers: list[str] = field(default_factory=list)
    plan_rounds: int = 0  # quantas vezes o Planner rodou (re_plan incrementa)

    # Reviewer L2 (contexto fresco) — objecoes voltam ao Coder, capadas.
    l2_objections: list[str] = field(default_factory=list)
    l2_retry_count: int = 0

    # ------------------------------------------------------------------
    # Fase 2 — budgets (WSB-E4-T1). `budget_max_usd` vem do JSONB
    # `work_items.budget` (chave "max_usd") lido na admissao; `spent_usd`
    # acumula o custo reportado pelo gateway (WS-D) em cada Activity de
    # modelo. Checado na admissao e em CADA fronteira de fase — nunca corta
    # no meio de uma Activity (P6).
    # ------------------------------------------------------------------
    budget_max_usd: float | None = None
    spent_usd: float = 0.0

    # ------------------------------------------------------------------
    # Caps/timers configuraveis (WSB-E3-T1 / E2-T3 / E5-T1). Fazem parte do
    # INPUT do workflow (nao de env-var lida dentro do sandbox) para que:
    #  (a) sejam deterministicas e sobrevivam a `continue_as_new`/replay;
    #  (b) os testes possam injetar timers curtos sem monkeypatch de env
    #      dentro do sandbox de workflow (que nao e seguro/suportado).
    # `dse_orchestrator.config.OrchestratorConfig.from_env()` e o helper que
    # quem inicia o workflow (worker/dispatcher do WS-A) usa para preencher
    # estes campos a partir do ambiente de producao.
    # ------------------------------------------------------------------
    clarification_round_cap: int = 3
    clarification_reminder_hours: float = 24.0
    clarification_escalation_days: float = 3.0
    coder_retry_cap: int = 3
    checkpoint_retry_cap: int = 2
    rebuild_retry_cap: int = 1
    activity_start_to_close_seconds: float = 3600.0
    activity_heartbeat_seconds: float = 30.0
    activity_schedule_to_close_seconds: float = 7200.0

    # Fase 2 (WSB-E3-T2/E3-T3/E4) — caps/politica novos. `require_approval_risk_classes`
    # e a POLITICA de quais classes de risco exigem aprovacao humana; vive no
    # INPUT (fora do modelo — P1), preenchida por config.from_env pelo caller.
    require_approval_risk_classes: tuple[str, ...] = ("high",)
    plan_round_cap: int = 3  # re_plan capado (rejection path — WSB-E3-T3)
    l2_retry_cap: int = 2  # objecoes do L2 -> Coder, capadas


@dataclass
class WorkItemLifecycleResult:
    work_item_id: str
    status: str
    detail: str | None = None
    pr_number: int | None = None


@dataclass
class OperatorEvent:
    """Ultimos eventos de operador, exposto via query para observabilidade."""

    action: str
    actor: str
    reason: str | None = None
