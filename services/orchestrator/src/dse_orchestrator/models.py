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
    # S1 (Fase 5): titulo+corpo da issue (ja sanitizado) — o QUE construir.
    # Carregado de ingest_events.payload por load_work_item; passado como
    # instruction do Planner/Coder. Sem isto os agentes nao sabiam a tarefa.
    task_content: str = ""
    # Numero da issue de origem (de work_items.source_ref, via load_work_item).
    # O finalize usa como issue_ref -> "Closes #N" no corpo do PR (back-link +
    # auto-close no merge). Sem isto o PR saia com "(sem issue de origem
    # vinculada)" mesmo vindo de uma issue.
    issue_number: int | None = None

    phase: str = PHASE_INTAKE
    status: str = "new"

    clarification_rounds: int = 0
    coder_retry_count: int = 0
    review_round: int = 0

    sandbox_id: str | None = None
    # S7 (Fase 5): SandboxHandle serializado (sandbox_id, work_item_id,
    # tenant_id, branch, container_id) retido do provision. L1 e finalize exigem
    # o handle COMPLETO (nao so o sandbox_id) para resolver o executor — o call
    # site antigo so guardava sandbox_id, causando `Failed decoding arguments`
    # (RunL1PipelineInput.sandbox faltando). Sobrevive a continue_as_new.
    sandbox_handle: dict = field(default_factory=dict)
    # Ultimo CheckpointRef (dict) bem-sucedido — fonte do rebuild. Vive no INPUT
    # (nao em atributo de instancia) para sobreviver a continue_as_new: um
    # rebuild na fase de review reconstroi do checkpoint da implementacao.
    last_checkpoint_ref: dict = field(default_factory=dict)
    branch: str | None = None
    # SHAs imutaveis delimitam todos os gates. Defaults preservam histories
    # antigos; execucoes novas capturam base_sha antes do primeiro turno e
    # head_sha em cada checkpoint anterior ao L1.
    base_sha: str | None = None
    head_sha: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    ci_status: str | None = None
    state_version: int = 0

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
    plan_hash: str | None = None
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
    # Remediação (spec §6): o substrato emite heartbeat periódico durante
    # turnos longos (sandbox_runtime.activity_heartbeat.run_sync_with_heartbeat).
    # 2026-07-23 (disparos reais wi_d05c/wi_257d/wi_150d): com 60s, turnos de
    # coder/tester com chamadas de modelo entravam em LOOP INFINITO de retry —
    # o server cancelava por heartbeat timeout ATIVIDADE JÁ TRABALHANDO (e o
    # retry re-pagava o modelo). 600s dá folga real; start_to_close (1h) segue
    # como teto duro do turno. NOTA: este default É o valor operativo no caminho
    # do dispatcher (start_workflow com string → _coerce_input usa o default do
    # modelo; apply_to_input/env só vale para quem passa o input completo).
    activity_heartbeat_seconds: float = 600.0
    activity_schedule_to_close_seconds: float = 7200.0
    # Retries de Activities de negocio devem terminar. O schedule-to-close
    # continua sendo o teto temporal, e este cap limita tentativas/custo.
    activity_retry_cap: int = 3

    # CI pending e uma espera duravel, nao permissao para review. O cap
    # default equivale a 24h com polling por minuto; ambos vivem no input para
    # replay deterministico e podem ser reduzidos nos testes.
    ci_poll_interval_seconds: float = 60.0
    ci_pending_poll_cap: int = 1440
    ci_pending_polls: int = 0

    # Fase 2 (WSB-E3-T2/E3-T3/E4) — caps/politica novos. `require_approval_risk_classes`
    # e a POLITICA de quais classes de risco exigem aprovacao humana; vive no
    # INPUT (fora do modelo — P1), preenchida por config.from_env pelo caller.
    require_approval_risk_classes: tuple[str, ...] = ("high",)
    plan_round_cap: int = 3  # re_plan capado (rejection path — WSB-E3-T3)
    l2_retry_cap: int = 2  # objecoes do L2 -> Coder, capadas

    # ------------------------------------------------------------------
    # Fase 3 — WSB-E4-T2: iteration caps + debounce de refresh de evidencia
    # (ADR-26) e o estado do pipeline de evidencia (preview/demo/visual diff)
    # que sobrevive a `continue_as_new` como o resto do estado deterministico.
    # ------------------------------------------------------------------
    # Cap de rounds de review (loop humano changes_requested/CI-red). Todo
    # `while` do workflow tem cap: clarificacao (clarification_round_cap),
    # fix L1 (coder_retry_cap), objecoes L2 (l2_retry_cap), re_plan
    # (plan_round_cap) e agora rounds de review. Esgotado -> escalated.
    review_round_cap: int = 20
    # Janela de debounce (segundos) para agrupar comentarios de review antes
    # de UM ciclo de fix + UM refresh de evidencia (ADR-26: 6 comentarios numa
    # janela = no maximo 1 refresh). 0 = sem janela (compat/testes legados);
    # producao recebe o valor de config.from_env via apply_to_input.
    evidence_debounce_seconds: float = 0.0
    # Cap de refreshes de evidencia ALEM do inicial. Excedido -> declina limpo
    # (auditado, P6) sem bloquear o PR — a evidencia apenas fica "stale".
    evidence_refresh_cap: int = 5
    evidence_refreshes: int = 0

    # Estado do pipeline de evidencia (projetado tambem em work_item_evidence,
    # migracao 0014, via Activity local record_evidence_state).
    preview_status: str | None = None   # PreviewRef.status
    preview_url: str | None = None
    evidence_passed: bool | None = None
    evidence_video_key: str | None = None
    evidence_trace_key: str | None = None
    visual_baseline_key: str | None = None
    # ultimo files_changed conhecido (Coder inicial ou ultimo fix cycle) — um
    # refresh a pedido humano nao tem commit novo, entao reusa este conjunto
    # para a decisao deterministica de paths-filter do trigger_preview (FR-20).
    last_files_changed: list[str] = field(default_factory=list)

    # Ativacao do alerta de history (infra/ALERTING-RULES.md §3, com WS-F):
    # contagem de Continue-As-New desta cadeia de execucoes, emitida junto com
    # o history length/size em cada fronteira via emit_history_metric.
    continue_as_new_count: int = 0

    # ------------------------------------------------------------------
    # Fase 4 — metricas de qualidade de PR (pilot gate "PR quality
    # thresholds", adendo 03). Contadores deterministicos que sobrevivem a
    # `continue_as_new` como o resto do estado; emitidos via OTel na fronteira
    # terminal do PR (merge/escalacao) pela Activity local emit_pr_quality_metric.
    # ------------------------------------------------------------------
    changes_requested_count: int = 0
    # epoch (segundos) de quando o PR foi finalizado (workflow.now() — leitura
    # deterministica/replay-safe). Usado para o tempo-ate-merge; None ate o PR
    # existir.
    pr_finalized_at_epoch: float | None = None


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
