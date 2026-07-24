"""WSB-E2/E3/E5 — `WorkItemLifecycleWorkflow`: a maquina de estados do
WorkItem (§9.3 da proposta), Fase 1 (sem Planner/Tester/Reviewer separados,
sem gate de aprovacao de plano por risk class, sem fairness/budget — isso e
Fase 2 / WSB-E4).

Fluxo: new -> needs_clarification (rounds capados) -> ready -> queued ->
implementing <-> validating (loop de fix capado) -> pr_ready <-> review_feedback
(loop humano, sem merge automatico em NENHUM path) -> done. blocked/failed/
escalated sao terminais e nunca "adivinham" o proximo passo.

Disciplina de determinismo (P1): todo I/O (Postgres, audit, Coder, L1, PR,
CI) vive em Activity. O corpo do `@workflow.run` so faz: aritmetica de
contagem, comparacao de strings/enums, `workflow.wait_condition`, e chamadas
a `workflow.execute_activity`/`workflow.continue_as_new`.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from datetime import timedelta
from typing import Any, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from dse_contracts.activities import (
        ACTIVITY_CHECKPOINT_SANDBOX,
        ACTIVITY_CONSUME_CI_STATUS,
        ACTIVITY_EMIT_AUDIT,
        ACTIVITY_FINALIZE_PR,
        ACTIVITY_POST_TRACKING_COMMENT,
        ACTIVITY_PROVISION_SANDBOX,
        ACTIVITY_REBUILD_SANDBOX,
        ACTIVITY_RUN_CODER_TURN,
        ACTIVITY_RUN_DEMO_EVIDENCE,
        ACTIVITY_RUN_L1_PIPELINE,
        ACTIVITY_RUN_L2_REVIEW,
        ACTIVITY_RUN_PLANNER_TURN,
        ACTIVITY_RUN_TESTER_TURN,
        ACTIVITY_RUN_VISUAL_DIFF,
        ACTIVITY_TEARDOWN_SANDBOX,
        ACTIVITY_TRIGGER_PREVIEW,
        ACTIVITY_UPDATE_BASE_BRANCH,
        ACTIVITY_VERIFY_MERGE_STATE,
        CheckpointRef,
        CiStatusResult,
        CoderTurnResult,
        DemoEvidenceResult,
        L1Result,
        L2Verdict,
        GateStatus,
        MergeVerification,
        PreviewRef,
        PrRef,
        SandboxHandle,
        TesterTurnResult,
        UpdateBaseBranchResult,
        VisualDiffResult,
    )
    from dse_contracts.constants import WORKFLOW_TYPE
    from dse_contracts.failure import FAIL_CLOSED_CLASSES, FailureClass, parse_failure_type
    from dse_contracts.plan_artifact import PlanArtifact
    from dse_contracts.work_item import MergedByHumanSignal, WorkItemStatus

    from dse_orchestrator import policy
    from dse_orchestrator.local_activities import (
        LOCAL_ACTIVITY_CHECK_CLARIFICATION,
        LOCAL_ACTIVITY_EMIT_HISTORY_METRIC,
        LOCAL_ACTIVITY_EMIT_PR_QUALITY_METRIC,
        LOCAL_ACTIVITY_LOAD_WORK_ITEM,
        LOCAL_ACTIVITY_POST_STATUS_TRANSITION,
        LOCAL_ACTIVITY_PREVIEW_ENABLED,
        LOCAL_ACTIVITY_RECORD_EVIDENCE,
        LOCAL_ACTIVITY_RECORD_GATE,
        LOCAL_ACTIVITY_RECORD_SKILL_EPISODE,
        LOCAL_ACTIVITY_RESOLVE_APPROVER,
        LOCAL_ACTIVITY_UPDATE_STATUS,
    )
    from dse_orchestrator.models import (
        PHASE_IMPLEMENTATION,
        PHASE_INTAKE,
        PHASE_REVIEW,
        OperatorEvent,
        WorkItemLifecycleInput,
        WorkItemLifecycleResult,
    )

logger = logging.getLogger("dse_orchestrator.workflow")

_SYSTEM_ACTOR = "system:orchestrator"
_MAX_OPERATOR_EVENTS = 25

# Fase 2 — status interno novo do gate de aprovacao de plano (WSB-E3-T2). NAO
# esta no enum `dse_contracts.work_item.WorkItemStatus` (fundacao — nao editavel
# por este workstream), mas a coluna `work_items.status` e TEXT sem CHECK, e a
# propria `constants.py` da fundacao ja referencia esta string como o valor de
# status que o dispatcher do WS-A consulta para rotear SIGNAL_PLAN_APPROVAL.
# Gap documentado no README ("Estado awaiting_plan_approval"): o enum da
# fundacao deveria ganhar este membro (+ mapa publico -> "blocked").
STATUS_AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"

# Fase 2 (plano 09): a classificação PRIMÁRIA é estruturada — o raiser declara
# a classe no `type` do ApplicationError (vocabulário dse.failure.* + legados;
# ver dse_contracts.failure e _failure_class_from). Esta lista de substrings é
# SÓ o fallback para histórias/raisers anteriores ao vocabulário — aposentar
# junto com o patch marker "structured-failure-codes-v1" na release seguinte.
_FAIL_CLOSED_MARKERS = (
    "egress",            # egress-proxy indisponivel -> zero egress (fail-closed)
    "fail_closed",
    "fail-closed",
    "policy_denied",
    "budget_denied",
    "virtual_key_expired",
    "key_expired",
    "kill_switch",
    # achado do disparo real (2026-07-22): créditos do provider esgotados NÃO é
    # falha transitória — o CLI mascarava como "error result: success" e a
    # activity retentava para sempre. Classificada non-retryable na origem
    # (sandbox_runtime) e reconhecida aqui -> falha limpa comentada na issue.
    "provider_billing",
    "credit balance",
)


def _failure_class_from(exc: Exception) -> "FailureClass | None":
    """Anda a cadeia de causas do erro da Activity lendo o `type` do
    ApplicationError (canônico dse.failure.* ou legado) — determinístico e
    sem I/O (seguro no sandbox do workflow). Profundidade capada."""
    cause: Exception | None = exc
    for _ in range(6):
        if cause is None:
            return None
        parsed = parse_failure_type(getattr(cause, "type", None))
        if parsed is not None:
            return parsed
        cause = getattr(cause, "cause", None) or getattr(cause, "__cause__", None)
    return None


class _CancelledByOperator(Exception):
    pass


class _ForceClarification(Exception):
    def __init__(self, reason: str):
        self.reason = reason


class _EscalateNow(Exception):
    def __init__(self, reason: str):
        self.reason = reason


class _FailClosed(Exception):
    """Recusa de politica do caminho de modelo/egress (WSB-E5-T3b) — falha
    limpa e auditada, sem output truncado (P6)."""

    def __init__(self, reason: str):
        self.reason = reason


class _BudgetExhausted(Exception):
    """WSB-E4-T1 — teto de budget atingido numa FRONTEIRA (nunca mid-Activity)."""

    def __init__(self, reason: str):
        self.reason = reason


class _PlanRejected(Exception):
    """WSB-E3-T3 — plano recusado; `route` in {re_plan, re_clarify, cancel}."""

    def __init__(self, route: str, actor: str, justification: str):
        self.route = route
        self.actor = actor
        self.justification = justification


class _BlockNow(Exception):
    """WSB-E3-T2 — cascata de aprovadores VAZIA: Blocked + escalacao, jamais
    auto-aprova por ausencia (P1/P3)."""

    def __init__(self, reason: str):
        self.reason = reason


class _ActivityRetriesExhausted(Exception):
    """Activity de negocio esgotou o cap; o workflow projeta falha terminal."""

    def __init__(self, activity_name: str, reason: str):
        self.activity_name = activity_name
        self.reason = reason


@workflow.defn(name=WORKFLOW_TYPE)
class WorkItemLifecycleWorkflow:
    def __init__(self) -> None:
        self._input: Optional[WorkItemLifecycleInput] = None

        # --- controles de operador (WSB-E5-T2) ---
        self._paused = False
        self._cancelled = False
        self._cancel_reason: str | None = None
        self._force_clarification_requested = False
        self._force_clarification_reason: str | None = None
        self._operator_escalate_requested = False
        self._operator_escalate_reason: str | None = None
        self._retry_from_checkpoint_requested = False
        self._model_override: str | None = None
        self._runtime_override: str | None = None
        self._operator_log: list[OperatorEvent] = []

        # --- sinais de negocio (WSB-E2-T4 / E3-T1 / E3-T4) ---
        self._clarification_received = False
        self._clarification_payload: dict[str, Any] | None = None
        self._review_received = False
        self._review_payload: dict[str, Any] | None = None
        # Fase 3 (WSB-E4-T2/ADR-26): comentarios de review ACUMULAM numa lista
        # (em vez de payload unico) para o loop de review consumir em LOTE —
        # N comentarios numa janela de debounce viram UM ciclo de fix + UM
        # refresh de evidencia, nunca N.
        self._review_comments: list[dict[str, Any]] = []
        self._merged = False
        self._merge_payload: dict[str, Any] | None = None
        # Fase 3 (ADR-26): pedido humano EXPLICITO de refresh de evidencia —
        # o unico gatilho de refresh alem de um fix cycle (commit novo).
        self._refresh_evidence_requested = False

        # --- Fase 2: gate de aprovacao de plano (WSB-E3-T2/T3) ---
        self._plan_approval_received = False
        self._plan_approval_payload: dict[str, Any] | None = None
        # --- Fase 2: retry de budget por operador (WSB-E4-T1) ---
        self._budget_raise_requested = False
        self._budget_new_max: float | None = None

        # --- Fase 4: deteccao de lacuna de clarificacao RECORRENTE (source do
        # insumo de skill-learning, WSC-E4-T2). Uniao dos campos que ja
        # faltaram em rounds anteriores DESTE run de intake — se um campo
        # reaparece faltando depois de ja termos pedido clarificacao, e uma
        # recorrencia e vira um skill_episode. Estado de instancia
        # (replay-safe: reconstruido deterministicamente pelos resultados das
        # Activities de completude no replay); nao precisa cruzar
        # continue_as_new porque o loop de intake completa dentro de um run.
        self._clarification_missing_union: set[str] = set()

    # ------------------------------------------------------------------
    # Signals — WSB-E2-T4, WSB-E3-T1, WSB-E3-T4, WSB-E5-T2
    # ------------------------------------------------------------------
    @workflow.signal
    def clarification_answer(self, payload: dict[str, Any]) -> None:
        self._clarification_payload = payload
        self._clarification_received = True

    @workflow.signal
    def review_comment(self, payload: dict[str, Any]) -> None:
        # Fase 3: acumula (nao sobrescreve) — o loop de review consome o LOTE
        # inteiro de uma vez (debounce ADR-26). `_review_payload` continua
        # guardando o ultimo, por compatibilidade de observabilidade/queries.
        self._review_comments.append(payload)
        self._review_payload = payload
        self._review_received = True

    @workflow.signal
    def refresh_evidence(self, payload: dict[str, Any] | None = None) -> None:
        """Fase 3 (ADR-26) — pedido humano EXPLICITO de re-geracao de evidencia
        (preview/demo/visual diff) sem commit novo. Junto com "fix cycle
        executado", e o UNICO gatilho de refresh — comentarios de review por si
        so NUNCA re-geram evidencia (decisao 100%% deterministica, P1)."""
        self._refresh_evidence_requested = True

    @workflow.signal
    def merged_by_human(self, payload: dict[str, Any] | None = None) -> None:
        self._merge_payload = payload or {}
        self._merged = True

    @workflow.signal(name="plan_approval")
    def plan_approval(self, payload: dict[str, Any]) -> None:
        """WSB-E3-T2/T3 — veredito do gate de aprovacao de plano. Nome bate com
        `dse_contracts.SIGNAL_PLAN_APPROVAL`. Payload: {"verdict":
        "approved"|"rejected", "route": "re_plan"|"re_clarify"|"cancel"
        (obrigatorio quando rejected), "comment"/"justification": str,
        "actor": principal resolvido}. O dispatcher do WS-A so roteia
        `kind=approval` para ca quando work_items.status ==
        'awaiting_plan_approval'. Nao resetamos a flag no handler (mesma
        disciplina anti-clobber dos outros sinais)."""
        self._plan_approval_payload = payload
        self._plan_approval_received = True

    @workflow.signal
    def raise_budget(self, new_max_usd: float) -> None:
        """WSB-E4-T1 — operador eleva o teto de budget. Aplicado na PROXIMA
        fronteira de checagem (nunca interrompe uma Activity em andamento, P6).
        Permite retomar um WorkItem que bateu (ou esta prestes a bater) o teto
        sem recomeçar do zero."""
        self._budget_raise_requested = True
        self._budget_new_max = float(new_max_usd)
        self._log_operator("raise_budget", str(new_max_usd))

    @workflow.signal
    def pause(self, reason: str | None = None) -> None:
        self._paused = True
        self._log_operator("pause", reason)

    @workflow.signal
    def resume(self, reason: str | None = None) -> None:
        self._paused = False
        self._log_operator("resume", reason)

    @workflow.signal
    def cancel(self, reason: str | None = None) -> None:
        self._cancelled = True
        self._cancel_reason = reason
        self._log_operator("cancel", reason)

    @workflow.signal
    def retry_from_checkpoint(self, reason: str | None = None) -> None:
        self._retry_from_checkpoint_requested = True
        self._log_operator("retry_from_checkpoint", reason)

    @workflow.signal
    def force_clarification(self, reason: str | None = None) -> None:
        self._force_clarification_requested = True
        self._force_clarification_reason = reason
        self._log_operator("force_clarification", reason)

    @workflow.signal
    def escalate(self, reason: str | None = None) -> None:
        self._operator_escalate_requested = True
        self._operator_escalate_reason = reason
        self._log_operator("escalate", reason)

    @workflow.signal
    def reassign_model(self, model: str) -> None:
        self._model_override = model
        self._log_operator("reassign_model", model)

    @workflow.signal
    def reassign_runtime(self, runtime: str) -> None:
        self._runtime_override = runtime
        self._log_operator("reassign_runtime", runtime)

    def _log_operator(self, action: str, reason: str | None) -> None:
        self._operator_log.append(OperatorEvent(action=action, actor="operator", reason=reason))
        if len(self._operator_log) > _MAX_OPERATOR_EVENTS:
            self._operator_log.pop(0)

    # ------------------------------------------------------------------
    # Queries — observabilidade/operador
    # ------------------------------------------------------------------
    @workflow.query
    def get_status(self) -> str:
        return self._input.status if self._input else "unknown"

    @workflow.query
    def get_state(self) -> dict[str, Any]:
        if self._input is None:
            return {}
        state = dataclasses.asdict(self._input)
        state["paused"] = self._paused
        state["cancelled"] = self._cancelled
        return state

    @workflow.query
    def get_operator_log(self) -> list[dict[str, Any]]:
        return [dataclasses.asdict(e) for e in self._operator_log]

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    @workflow.run
    async def run(self, raw_input: Any) -> WorkItemLifecycleResult:
        input = await self._coerce_input(raw_input)
        self._input = input
        try:
            if input.phase == PHASE_INTAKE:
                return await self._run_intake_phase()
            elif input.phase == PHASE_IMPLEMENTATION:
                return await self._run_implementation_phase()
            elif input.phase == PHASE_REVIEW:
                return await self._run_review_phase()
            else:
                # fase terminal ja resolvida (defensivo — nao deveria acontecer)
                return WorkItemLifecycleResult(
                    work_item_id=input.work_item_id,
                    status=input.status,
                    detail=input.terminal_detail,
                    pr_number=input.pr_number,
                )
        except _CancelledByOperator:
            return await self._finish_cancelled()
        except _EscalateNow as exc:
            return await self._finish_escalated(exc.reason)
        except _FailClosed as exc:
            # WSB-E5-T3b — recusa de politica do caminho de modelo/egress:
            # falha limpa e auditada, sem output truncado (P6).
            return await self._finish_failed(f"fail_closed:{exc.reason}",
                                             audit_action="model_path_fail_closed")
        except _BudgetExhausted as exc:
            return await self._finish_failed(f"budget_exhausted:{exc.reason}",
                                             audit_action="budget_exhausted")
        except _PlanRejected as exc:
            return await self._handle_plan_rejection(exc)
        except _BlockNow as exc:
            return await self._finish_blocked(exc.reason, audit_action="plan_gate_blocked_no_approver")
        except _ActivityRetriesExhausted as exc:
            return await self._finish_failed(
                f"activity_retries_exhausted:{exc.activity_name}:{exc.reason}",
                audit_action="activity_retries_exhausted",
            )
        except _ForceClarification as exc:
            input.phase = PHASE_INTAKE
            input.status = WorkItemStatus.needs_clarification.value
            input.clarification_rounds = 0
            input.terminal_detail = f"force_clarification: {exc.reason}"
            await self._audit("force_clarification_applied", {"reason": exc.reason})
            await self._persist_status()
            return await self._continue_as_new(input)

    # ------------------------------------------------------------------
    # Helpers genericos
    # ------------------------------------------------------------------
    async def _coerce_input(self, raw_input: Any) -> WorkItemLifecycleInput:
        """Robustez de integracao (achado real ao testar contra o worker de
        WS-A): quem inicia o workflow pode chamar `StartWorkflow` so com o
        `work_item_id` (string) em vez do `WorkItemLifecycleInput` completo
        — por exemplo `client.start_workflow(WORKFLOW_TYPE, work_item_id,
        id=work_item_id, task_queue=...)`. Continue_as_new interno SEMPRE
        passa a dataclass completa, entao esse ramo so roda no PRIMEIRO
        `run()` de uma execucao vinda de fora. Ver README, secao "Contrato de
        start_workflow assumido"."""
        if isinstance(raw_input, WorkItemLifecycleInput):
            return raw_input
        if isinstance(raw_input, dict):
            return WorkItemLifecycleInput(**raw_input)
        if isinstance(raw_input, str):
            row = await workflow.execute_activity(
                LOCAL_ACTIVITY_LOAD_WORK_ITEM,
                {"work_item_id": raw_input},
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
            budget = row.get("budget") or {}
            max_usd = budget.get("max_usd") if isinstance(budget, dict) else None
            return WorkItemLifecycleInput(
                work_item_id=row["work_item_id"],
                tenant_id=row["tenant_id"],
                requester=row["requester"],
                repo=row.get("repo"),
                base_branch=row.get("base_branch"),
                data_class=row.get("data_class") or "internal",
                pr_number=row.get("pr_number"),
                risk_class=row.get("risk_class") or "low",
                plan_json=row.get("plan") or {},
                plan_hash=row.get("plan_hash"),
                base_sha=row.get("base_sha"),
                head_sha=row.get("head_sha"),
                pr_url=row.get("pr_url"),
                ci_status=row.get("ci_status"),
                state_version=int(row.get("state_version") or 0),
                task_content=row.get("task_content") or "",
                issue_number=(int(row["issue_number"]) if row.get("issue_number") else None),
                budget_max_usd=float(max_usd) if max_usd is not None else None,
            )
        raise ApplicationError(
            f"WorkItemLifecycleWorkflow.run recebeu um tipo de input nao suportado: {type(raw_input)!r}"
        )

    def _agent_instruction(self, *, include_objections: bool = False) -> str:
        """S1 (Fase 5): monta a instrucao REAL que o Planner/Coder recebem —
        a descricao da tarefa (titulo+corpo da issue), mais criterio de aceite
        e respostas de clarificacao, mais (para o Coder no fix loop) as
        objecoes do L2. Determinístico (P1): so concatena strings ja
        capturadas, nenhum LLM decide o conteudo. Antes do S1 os agentes
        recebiam so `clarification_notes` (vazio) e nao sabiam a tarefa."""
        input = self._input
        parts: list[str] = []
        if (input.task_content or "").strip():
            parts.append(input.task_content.strip())
        if (input.acceptance_criteria or "").strip():
            parts.append(f"Acceptance criteria: {input.acceptance_criteria.strip()}")
        for note in input.clarification_notes:
            if note and note.strip():
                parts.append(f"Clarification: {note.strip()}")
        if include_objections:
            for obj in getattr(input, "l2_objections", []) or []:
                if obj and str(obj).strip():
                    parts.append(f"Review objection to fix: {str(obj).strip()}")
        return "\n\n".join(parts) or "Implementar a tarefa solicitada."

    def _pr_summary(self) -> str:
        """S7 (Fase 5): corpo do PR (campo `summary` do FinalizePrInput).
        Determinístico (P1): usa o `summary` do plano se houver, senão a 1a
        linha do conteúdo da tarefa. Nenhum LLM decide aqui."""
        input = self._input
        plan_summary = None
        if isinstance(input.plan_json, dict):
            plan_summary = (input.plan_json.get("summary") or "").strip() or None
        title = ""
        if (input.task_content or "").strip():
            title = input.task_content.strip().splitlines()[0].strip()
        return plan_summary or title or f"DSE: automated change ({input.work_item_id})"

    @staticmethod
    def _l1_evidence(l1_result) -> str:
        """S7 follow-up: evidência de teste no corpo do PR (P8 — evidência
        sobre asserção). Neste ponto a evidência real que EXISTE é o resultado
        do L1 (o pipeline de vídeo/preview roda DEPOIS do PR); resume os checks
        que passaram, deterministicamente. Sem isto o PR saía com
        "(sem link de evidência)"."""
        if l1_result is None or not getattr(l1_result, "findings", None):
            return ""
        checks = ", ".join(f"{f.check} ✓" for f in l1_result.findings if f.passed)
        return f"L1 green ({checks})" if checks else ""

    async def _boundary_gate(self) -> None:
        """Checado antes de CADA Activity de negocio (nao antes de bookkeeping
        local). Pausa nunca interrompe uma Activity em andamento — so atrasa
        a proxima chamada (WSB-E5-T2)."""
        await workflow.wait_condition(lambda: (not self._paused) or self._cancelled)
        if self._cancelled:
            raise _CancelledByOperator()
        if self._operator_escalate_requested:
            raise _EscalateNow(self._operator_escalate_reason or "operator_escalate")
        if self._force_clarification_requested:
            raise _ForceClarification(self._force_clarification_reason or "operator_requested")

    async def _audit(self, action: str, details: dict[str, Any] | None = None) -> None:
        payload = {
            "actor": _SYSTEM_ACTOR,
            "action": action,
            "tenant_id": self._input.tenant_id,
            "work_item_id": self._input.work_item_id,
            "details": details or {},
        }
        await workflow.execute_activity(
            ACTIVITY_EMIT_AUDIT,
            payload,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )

    async def _persist_status(self, *, clear_ci_status: bool = False) -> None:
        # Patch marker preserva replay de histories cujo payload antigo tinha
        # somente work_item_id/status/pr_number. Novas execucoes projetam toda
        # a verdade operacional; o servidor deriva plan_hash/expected_files.
        if workflow.patched("operational-state-payload-v1"):
            payload = {
                "work_item_id": self._input.work_item_id,
                "status": self._input.status,
                "pr_number": self._input.pr_number,
                "pr_url": self._input.pr_url,
                "plan": self._input.plan_json or None,
                "risk_class": self._input.risk_class,
                "base_sha": self._input.base_sha,
                "head_sha": self._input.head_sha,
                "ci_status": self._input.ci_status,
                "clear_ci_status": clear_ci_status,
                "last_error": self._input.terminal_detail,
                "validation_attempts": {
                    "coder": self._input.coder_retry_count,
                    "l2": self._input.l2_retry_count,
                    "review": self._input.review_round,
                    "ci_pending": self._input.ci_pending_polls,
                },
            }
        else:  # replay de payload historico
            payload = {
                "work_item_id": self._input.work_item_id,
                "status": self._input.status,
                "pr_number": self._input.pr_number,
            }
        persisted = await workflow.execute_activity(
            LOCAL_ACTIVITY_UPDATE_STATUS,
            payload,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )
        if isinstance(persisted, dict):
            self._input.state_version = int(
                persisted.get("state_version") or self._input.state_version
            )
            self._input.plan_hash = persisted.get("plan_hash") or self._input.plan_hash

    async def _set_status(self, status: WorkItemStatus, *, audit_action: str | None = None,
                           details: dict[str, Any] | None = None,
                           clear_ci_status: bool = False) -> None:
        self._input.status = status.value
        await self._persist_status(clear_ci_status=clear_ci_status)
        if audit_action:
            await self._audit(audit_action, details)

    async def _set_raw_status(self, status: str, *, audit_action: str | None = None,
                              details: dict[str, Any] | None = None) -> None:
        """Como `_set_status`, mas para um valor de status que nao esta no enum
        `WorkItemStatus` da fundacao (ex.: `awaiting_plan_approval` — ver
        STATUS_AWAITING_PLAN_APPROVAL e o README). A coluna e TEXT, aceita a
        string; o gap do enum esta documentado no README."""
        self._input.status = status
        await self._persist_status()
        if audit_action:
            await self._audit(audit_action, details)

    def _activity_timeouts(self) -> dict[str, Any]:
        return dict(
            start_to_close_timeout=timedelta(seconds=self._input.activity_start_to_close_seconds),
            heartbeat_timeout=timedelta(seconds=self._input.activity_heartbeat_seconds),
            schedule_to_close_timeout=timedelta(seconds=self._input.activity_schedule_to_close_seconds),
        )

    async def _finish_cancelled(self) -> WorkItemLifecycleResult:
        if self._input.sandbox_id:
            try:
                await workflow.execute_activity(
                    ACTIVITY_TEARDOWN_SANDBOX,
                    # Boundary fix (auditoria pós-S7): TeardownSandboxInput exige
                    # tenant_id — sem ele o decode falhava e NENHUM teardown rodava
                    # (sandboxes órfãos). `reason` vira `stage` (campo real do modelo).
                    {
                        "work_item_id": self._input.work_item_id,
                        "tenant_id": self._input.tenant_id,
                        "stage": "cancelled_by_operator",
                    },
                    start_to_close_timeout=timedelta(seconds=120),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
            except ActivityError:
                logger.warning("teardown falhou durante cancelamento; seguindo mesmo assim")
        detail = f"cancelled_by_operator: {self._cancel_reason or 'no reason given'}"
        self._input.terminal_detail = detail
        await self._set_status(
            WorkItemStatus.failed,
            audit_action="cancelled_by_operator",
            details={"reason": self._cancel_reason},
        )
        await self._post_status_comment("failed", detail=detail)
        return WorkItemLifecycleResult(
            work_item_id=self._input.work_item_id,
            status=WorkItemStatus.failed.value,
            detail=detail,
            pr_number=self._input.pr_number,
        )

    async def _post_status_comment(self, status: str, *, detail: str = "") -> None:
        """Oversight barato (nunca deixar uma superfície sem o estado atual): toda
        transição consequencial reflete o status no comentário único da superfície
        de origem. BEST-EFFORT — jamais bloqueia a transição (o audit ledger e a
        fonte de verdade; o comentario e conveniencia). A Activity resolve
        repo/issue e e no-op auditado para origens nao-github (Slack/Jira terao
        seu proprio caminho de saida com o MESMO vocabulario de status)."""
        # Guard de replay (spec §5): a superficie de status foi adicionada
        # depois — histories antigas NAO tem esta Activity e devem replayar sem
        # ela. UM marker cobre TODAS as transicoes (terminais + implementing);
        # execucoes novas postam, replays antigos pulam deterministicamente.
        if not workflow.patched("status-comment-surfacing-v1"):
            return
        try:
            await workflow.execute_activity(
                ACTIVITY_POST_TRACKING_COMMENT,
                {"work_item_id": self._input.work_item_id, "tenant_id": self._input.tenant_id,
                 "status": status, "detail": detail},
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
        except ActivityError:
            logger.warning("post_status_comment best-effort falhou (status=%s)", status)
        # Fase B: reflete tambem no BOARD (transição de coluna do Jira). No-op
        # para github/slack. Guard novo (patch proprio) para replay-safety —
        # histories entre a superficie de status e o board pulam so esta parte.
        if workflow.patched("status-transition-board-v1"):
            try:
                await workflow.execute_activity(
                    LOCAL_ACTIVITY_POST_STATUS_TRANSITION,
                    {"work_item_id": self._input.work_item_id,
                     "tenant_id": self._input.tenant_id, "status": status},
                    start_to_close_timeout=timedelta(seconds=60),
                    retry_policy=RetryPolicy(maximum_attempts=5),
                )
            except ActivityError:
                logger.warning("post_status_transition best-effort falhou (status=%s)", status)

    async def _post_board_transition(self, status: str) -> None:
        """Move o card do Jira para a coluna mapeada, SEM re-postar o comentario
        de status. Existe porque `_post_status_comment` so era chamado em
        `implementing` e nos estados de erro — o card ia para In Progress mas
        NUNCA para In Review (PR aberto) nem Done (merge). Achado BD-39
        (2026-07-23). Best-effort, no-op para github/slack (a Activity resolve
        origem), guard de patch proprio para replay-safety de workflows em voo."""
        if not workflow.patched("board-transition-pr-and-done-v1"):
            return
        try:
            await workflow.execute_activity(
                LOCAL_ACTIVITY_POST_STATUS_TRANSITION,
                {"work_item_id": self._input.work_item_id,
                 "tenant_id": self._input.tenant_id, "status": status},
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
        except ActivityError:
            logger.warning("post_board_transition best-effort falhou (status=%s)", status)

    async def _finish_escalated(self, reason: str) -> WorkItemLifecycleResult:
        detail = f"escalated: {reason}"
        self._input.terminal_detail = detail
        await self._set_status(
            WorkItemStatus.escalated,
            audit_action="escalated",
            details={"reason": reason},
        )
        await self._post_status_comment("escalated", detail=reason)
        # Fase 4 — se ja havia um PR finalizado, esta escalacao e uma fronteira
        # terminal do PR: emite a metrica de qualidade (pilot gate) para nao
        # perder dados de PRs que nao mergeiam. Escalacoes pre-PR (ex.:
        # clarificacao) nao tem PR e sao puladas.
        if self._input.pr_finalized_at_epoch is not None:
            await self._emit_pr_quality_metric("escalated")
        return WorkItemLifecycleResult(
            work_item_id=self._input.work_item_id,
            status=WorkItemStatus.escalated.value,
            detail=detail,
            pr_number=self._input.pr_number,
        )

    async def _finish_failed(self, reason: str, *, audit_action: str) -> WorkItemLifecycleResult:
        """Falha terminal LIMPA (P6): status Failed + audit, sem output
        truncado. Faz teardown do sandbox se existir (best-effort)."""
        if self._input.sandbox_id:
            try:
                await workflow.execute_activity(
                    ACTIVITY_TEARDOWN_SANDBOX,
                    {"work_item_id": self._input.work_item_id,
                     "tenant_id": self._input.tenant_id, "stage": audit_action},
                    start_to_close_timeout=timedelta(seconds=120),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
            except ActivityError:
                logger.warning("teardown falhou durante falha terminal; seguindo mesmo assim")
        detail = reason
        self._input.terminal_detail = detail
        await self._set_status(
            WorkItemStatus.failed, audit_action=audit_action, details={"reason": reason}
        )
        await self._post_status_comment("failed", detail=reason)
        return WorkItemLifecycleResult(
            work_item_id=self._input.work_item_id,
            status=WorkItemStatus.failed.value,
            detail=detail,
            pr_number=self._input.pr_number,
        )

    async def _finish_blocked(self, reason: str, *, audit_action: str) -> WorkItemLifecycleResult:
        """Estado terminal Blocked (WSB-E3-T2: cascata de aprovadores vazia).
        Distinto de Failed — o WorkItem esta esperando intervencao humana
        (nomear um aprovador), nao morto. Escala junto (audit)."""
        detail = reason
        self._input.terminal_detail = detail
        await self._set_status(
            WorkItemStatus.blocked, audit_action=audit_action, details={"reason": reason}
        )
        await self._audit("escalated", {"reason": reason})
        await self._post_status_comment("blocked", detail=reason)
        return WorkItemLifecycleResult(
            work_item_id=self._input.work_item_id,
            status=WorkItemStatus.blocked.value,
            detail=detail,
            pr_number=self._input.pr_number,
        )

    # ------------------------------------------------------------------
    # Budgets (WSB-E4-T1) — deterministico, puro (aritmetica/comparacao),
    # seguro no corpo do workflow. Checado na admissao e em CADA fronteira.
    # ------------------------------------------------------------------
    def _apply_pending_budget_raise(self) -> None:
        if self._budget_raise_requested and self._budget_new_max is not None:
            self._input.budget_max_usd = self._budget_new_max
            self._budget_raise_requested = False
            self._budget_new_max = None

    async def _budget_boundary(self, boundary: str) -> None:
        """Checa o teto de budget numa FRONTEIRA. Se ja aplicou um raise
        pendente, usa o novo teto. Estourou -> `_BudgetExhausted` (nunca corta
        no meio de uma Activity — so aqui, entre elas). Toda checagem audita."""
        self._apply_pending_budget_raise()
        cap = self._input.budget_max_usd
        if cap is None:
            return  # sem teto configurado: nada a impor
        if self._input.spent_usd >= cap:
            await self._audit(
                "budget_boundary_denied",
                {"boundary": boundary, "spent_usd": round(self._input.spent_usd, 6),
                 "max_usd": cap},
            )
            raise _BudgetExhausted(
                f"boundary={boundary} spent={round(self._input.spent_usd, 6)}>=max={cap}"
            )
        await self._audit(
            "budget_boundary_ok",
            {"boundary": boundary, "spent_usd": round(self._input.spent_usd, 6), "max_usd": cap},
        )

    async def _consume_cost(self, cost_usd: float, *, source: str) -> None:
        """Agrega o custo reportado pelo gateway (WS-D) via o resultado da
        Activity. Todo consumo audita (P8)."""
        if not cost_usd:
            return
        self._input.spent_usd += float(cost_usd)
        await self._audit(
            "budget_consumed",
            {"source": source, "delta_usd": float(cost_usd),
             "spent_usd": round(self._input.spent_usd, 6)},
        )

    async def _run_model_activity(self, name: str, payload: dict[str, Any], result_type):
        """Wrapper das Activities do caminho de MODELO/SANDBOX (planner/coder/
        tester/l2/provision/l1). Converte uma recusa de politica fail-closed
        (egress-proxy down, virtual key expirada, kill switch) em `_FailClosed`
        — falha limpa e auditada, sem output truncado (P6). Erros
        retryable/transientes continuam sendo retentados pelo proprio Temporal
        (nao caem aqui: so ActivityError final chega)."""
        try:
            options = self._activity_timeouts()
            if workflow.patched("model-activity-infra-retry-v2"):
                # spec §6: DISTINGUIR falha de infra retryable de permanente.
                # Falhas permanentes (fail-closed do egress/gateway, virtual key
                # expirada, kill switch, decode de contrato) sao lancadas
                # `non_retryable` na origem e NAO retentam — falham rapido.
                # As retryable (oscilacao transiente do gateway) retentam sob o
                # teto de WALL-CLOCK (schedule_to_close, ~2h) + o budget por fase
                # (custo) — nunca sob um cap de CONTAGEM que confunde um blip de
                # infra com uma falha permanente e mata a durabilidade (P6/P8:
                # 3 quedas transientes nao podem falhar uma tarefa cara).
                options["retry_policy"] = RetryPolicy(maximum_attempts=0)
            elif workflow.patched("bounded-business-activity-retries-v1"):
                options["retry_policy"] = RetryPolicy(
                    maximum_attempts=max(1, int(self._input.activity_retry_cap))
                )
            return await workflow.execute_activity(
                name, payload, result_type=result_type, **options
            )
        except ActivityError as exc:
            blob = f"{type(exc.cause).__name__}:{exc.cause}".lower() if exc.cause else str(exc).lower()
            fail_class = None
            if workflow.patched("structured-failure-codes-v1"):
                # Fase 2 (plano 09): a decisão vem do `type` estruturado do
                # ApplicationError — a MENSAGEM não decide mais nada.
                fail_class = _failure_class_from(exc)
            if fail_class is None and any(marker in blob for marker in _FAIL_CLOSED_MARKERS):
                # fallback (histórias/raisers pré-vocabulário) — aposentar na
                # release seguinte junto com _FAIL_CLOSED_MARKERS.
                fail_class = FailureClass.policy_fail_closed
            if fail_class is not None and fail_class in FAIL_CLOSED_CLASSES:
                await self._audit(
                    "model_path_fail_closed_detected",
                    {"activity": name, "failure_class": fail_class.value, "error": blob[:300]},
                )
                raise _FailClosed(f"{name}:{blob[:200]}")
            raise _ActivityRetriesExhausted(name, blob[:300])

    async def _capture_base_sha(self) -> None:
        """Captura o tip imutavel anterior ao primeiro turno do Coder.

        Reusa o checkpoint deterministico existente: logo apos o provision o
        task branch ainda aponta exatamente para a base. O SHA retornado vira
        base_sha e head_sha inicial, persistidos antes de qualquer escrita.
        """
        input = self._input
        try:
            ref: CheckpointRef = await workflow.execute_activity(
                ACTIVITY_CHECKPOINT_SANDBOX,
                {
                    "sandbox_id": input.sandbox_id,
                    "work_item_id": input.work_item_id,
                    "tenant_id": input.tenant_id,
                    "branch": input.branch,
                    "phase": "base",
                },
                result_type=CheckpointRef,
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=RetryPolicy(
                    maximum_attempts=max(1, int(input.activity_retry_cap))
                ),
            )
        except ActivityError as exc:
            raise _ActivityRetriesExhausted(
                ACTIVITY_CHECKPOINT_SANDBOX, str(exc.cause or exc)[:300]
            )
        input.base_sha = ref.git_ref
        input.head_sha = ref.git_ref
        input.last_checkpoint_ref = ref.model_dump()
        if input.sandbox_handle:
            input.sandbox_handle["base_sha"] = input.base_sha
            input.sandbox_handle["head_sha"] = input.head_sha
        await self._persist_status()
        await self._audit("base_sha_captured", {"base_sha": input.base_sha})

    async def _checkpoint_or_rebuild(self, phase_name: str) -> None:
        """WSB-E5-T1: checkpoint ao fim de cada fase, com retries limitados;
        esgotado, tenta rebuild; esgotado tambem, escala (nunca segue
        adivinhando com um sandbox potencialmente corrompido)."""
        if not self._input.sandbox_id:
            return
        for _ in range(self._input.checkpoint_retry_cap):
            try:
                # S7 (Fase 5): CheckpointSandboxInput exige `tenant_id` (o call
                # site antigo nao o enviava — boundary bug latente escondido
                # pelos fakes; mesma classe dos outros). Capturamos o
                # CheckpointRef retornado para o rebuild poder reconstruir dele.
                ref: CheckpointRef = await workflow.execute_activity(
                    ACTIVITY_CHECKPOINT_SANDBOX,
                    {
                        "sandbox_id": self._input.sandbox_id,
                        "work_item_id": self._input.work_item_id,
                        "tenant_id": self._input.tenant_id,
                        "phase": phase_name,
                    },
                    result_type=CheckpointRef,
                    start_to_close_timeout=timedelta(seconds=120),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
                self._input.last_checkpoint_ref = ref.model_dump()
                self._input.head_sha = ref.git_ref
                if self._input.sandbox_handle:
                    self._input.sandbox_handle["head_sha"] = ref.git_ref
                return
            except ActivityError:
                continue
        await self._audit("checkpoint_exhausted_attempting_rebuild", {"phase": phase_name})
        # rebuild so e possivel se ha um checkpoint anterior de onde reconstruir.
        if not self._input.last_checkpoint_ref:
            raise _EscalateNow(f"checkpoint_failed_no_prior_ref:{phase_name}")
        for _ in range(self._input.rebuild_retry_cap):
            try:
                handle: SandboxHandle = await workflow.execute_activity(
                    ACTIVITY_REBUILD_SANDBOX,
                    {"work_item_id": self._input.work_item_id, "tenant_id": self._input.tenant_id,
                     "checkpoint_ref": self._input.last_checkpoint_ref},
                    result_type=SandboxHandle,
                    start_to_close_timeout=timedelta(seconds=300),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
                self._input.sandbox_id = handle.sandbox_id
                self._input.sandbox_handle = handle.model_dump()
                if self._input.base_sha:
                    self._input.sandbox_handle["base_sha"] = self._input.base_sha
                if self._input.head_sha:
                    self._input.sandbox_handle["head_sha"] = self._input.head_sha
                await self._audit("rebuild_succeeded", {"phase": phase_name})
                return
            except ActivityError:
                continue
        raise _EscalateNow(f"checkpoint_and_rebuild_exhausted:{phase_name}")

    async def _continue_as_new(self, input: WorkItemLifecycleInput):
        """Todo `continue_as_new` passa por aqui: incrementa a contagem de
        Continue-As-New (parte da metrica de history — ALERTING-RULES.md §3)
        e emite a metrica uma ultima vez antes de fechar o run atual."""
        input.continue_as_new_count += 1
        await self._emit_history_metric("continue_as_new")
        return workflow.continue_as_new(input)

    async def _emit_history_metric(self, checkpoint: str) -> None:
        """Fase 3 — ativacao do alerta de history (WS-F ativa a regra §3).
        Leitura DETERMINISTICA no workflow (get_current_history_length/size
        sao replay-safe no SDK); emissao OTel na Activity local. Best-effort:
        falha de metrica jamais afeta o fluxo."""
        info = workflow.info()
        payload = {
            "work_item_id": self._input.work_item_id,
            "tenant_id": self._input.tenant_id,
            "phase": self._input.phase,
            "checkpoint": checkpoint,
            "history_length": info.get_current_history_length(),
            "history_size_bytes": info.get_current_history_size(),
            "continue_as_new_count": self._input.continue_as_new_count,
        }
        try:
            await workflow.execute_activity(
                LOCAL_ACTIVITY_EMIT_HISTORY_METRIC,
                payload,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError:
            logger.warning("emit_history_metric falhou; metrica e best-effort, fluxo segue")

    async def _maybe_retry_from_checkpoint(self) -> None:
        if not self._retry_from_checkpoint_requested:
            return
        self._retry_from_checkpoint_requested = False
        await self._audit("retry_from_checkpoint_applied", {})
        # Boundary fix (auditoria pós-S7): RebuildSandboxInput exige tenant_id +
        # checkpoint_ref — o payload antigo {work_item_id, sandbox_id} falhava no
        # decode. Sem checkpoint anterior nesta fase, escala limpo (P6) em vez de
        # reconstruir de lugar nenhum.
        if not self._input.last_checkpoint_ref:
            raise _EscalateNow("retry_from_checkpoint_no_prior_ref")
        try:
            handle: SandboxHandle = await workflow.execute_activity(
                ACTIVITY_REBUILD_SANDBOX,
                {"work_item_id": self._input.work_item_id, "tenant_id": self._input.tenant_id,
                 "checkpoint_ref": self._input.last_checkpoint_ref},
                result_type=SandboxHandle,
                start_to_close_timeout=timedelta(seconds=300),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
            self._input.sandbox_id = handle.sandbox_id
        except ActivityError:
            raise _EscalateNow("retry_from_checkpoint_failed")

    # ------------------------------------------------------------------
    # Fase 1 — intake / gate de clarificacao (WSB-E3-T1)
    # ------------------------------------------------------------------
    async def _run_intake_phase(self) -> WorkItemLifecycleResult:
        input = self._input

        if input.status == WorkItemStatus.new.value:
            await self._audit("intake_started")

        while True:
            await self._boundary_gate()

            completeness = await workflow.execute_activity(
                LOCAL_ACTIVITY_CHECK_CLARIFICATION,
                {
                    "repo": input.repo,
                    "base_branch": input.base_branch,
                    "acceptance_criteria": input.acceptance_criteria,
                    # S2 (Fase 5): o conteudo da tarefa entra no checklist —
                    # uma issue bem descrita satisfaz o "o que fazer" sem exigir
                    # um campo acceptance_criteria separado.
                    "task_content": input.task_content,
                },
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
            if completeness["complete"]:
                await self._set_status(WorkItemStatus.ready, audit_action="clarification_complete")
                input.phase = PHASE_IMPLEMENTATION
                input.status = WorkItemStatus.queued.value
                return await self._continue_as_new(input)

            if input.clarification_rounds >= self._input.clarification_round_cap:
                raise _EscalateNow(
                    f"clarification_round_cap_exhausted (missing={completeness['missing']})"
                )

            # Fase 4 (WSC-E4-T2, source=clarification) — deteccao de lacuna
            # RECORRENTE: um campo que continua faltando DEPOIS de ja termos
            # pedido clarificacao ao menos uma vez neste intake. Puro set-arith
            # (deterministico, P1); a escrita do insumo vive numa Activity.
            # NENHUMA skill e criada aqui — so o episodio, que o WS-C consome.
            missing = list(completeness["missing"])
            recurring = sorted(set(missing) & self._clarification_missing_union)
            if recurring:
                await self._emit_clarification_episode(recurring, missing)
            self._clarification_missing_union |= set(missing)

            await self._set_status(
                WorkItemStatus.needs_clarification,
                audit_action="clarification_requested",
                details={"missing": completeness["missing"], "round": input.clarification_rounds},
            )

            # S3 (Fase 5): posta a PERGUNTA de volta na superfície de origem
            # (issue do GitHub), enumerando os campos faltantes — antes disto o
            # humano nunca via o que era pedido. Best-effort (nunca derruba o
            # workflow); a Activity auto-resolve repo/issue_number pelo work_item.
            _field_labels = {"acceptance_criteria": "acceptance criteria / expected behavior",
                             "repo": "repository", "base_branch": "base branch"}
            question = "I need the following information before I can start: " + ", ".join(
                _field_labels.get(m, m) for m in completeness["missing"]
            ) + ". Reply in this thread and I will resume automatically."
            try:
                await workflow.execute_activity(
                    ACTIVITY_POST_TRACKING_COMMENT,
                    {"work_item_id": input.work_item_id, "tenant_id": input.tenant_id,
                     "status": "needs_clarification", "detail": question},
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
            except ActivityError:
                logger.warning("post_tracking_comment (clarificacao) falhou; workflow segue esperando resposta")

            # IMPORTANTE: nao reset `_clarification_received` aqui — um
            # `clarification_answer` pode ja ter chegado durante os awaits
            # acima (activity de completude/status/audit); resetar aqui
            # apagaria uma resposta legitima (mesmo bug de "clobber" do loop
            # de review — ver `_run_review_phase`). So consumimos e
            # resetamos DEPOIS de ler o payload, abaixo.
            reminder_delta = timedelta(hours=self._input.clarification_reminder_hours)
            escalation_delta = timedelta(days=self._input.clarification_escalation_days)
            remaining_after_reminder = escalation_delta - reminder_delta
            if remaining_after_reminder.total_seconds() < 0:
                remaining_after_reminder = timedelta(seconds=0)

            got_answer = await self._wait_with_reminder(
                condition=lambda: self._clarification_received or self._cancelled
                or self._operator_escalate_requested or self._force_clarification_requested,
                reminder_delta=reminder_delta,
                remaining_delta=remaining_after_reminder,
                reminder_action="clarification_reminder_sent",
            )
            if self._cancelled:
                raise _CancelledByOperator()
            if self._operator_escalate_requested:
                raise _EscalateNow(self._operator_escalate_reason or "operator_escalate")
            if self._force_clarification_requested:
                self._force_clarification_requested = False  # ja estamos no intake

            if not got_answer:
                raise _EscalateNow("clarification_no_response_after_reminder_and_escalation_window")

            payload = self._clarification_payload or {}
            self._clarification_received = False  # consumido — proxima volta espera sinal NOVO
            if payload.get("repo"):
                input.repo = payload["repo"]
            if payload.get("base_branch"):
                input.base_branch = payload["base_branch"]
            if payload.get("acceptance_criteria"):
                input.acceptance_criteria = payload["acceptance_criteria"]
            if payload.get("text"):
                input.clarification_notes.append(payload["text"])

            input.clarification_rounds += 1
            await self._audit("clarification_answer_received", {"round": input.clarification_rounds})
            # volta ao topo do loop para re-checar completude (sem LLM: puro checklist)

    async def _wait_with_reminder(
        self,
        *,
        condition,
        reminder_delta: timedelta,
        remaining_delta: timedelta,
        reminder_action: str,
    ) -> bool:
        """`True` se `condition()` ficou verdadeira dentro da janela total
        (reminder + escalation); `False` se estourou o prazo total sem
        resposta (o caller decide o que fazer — nunca segue adivinhando)."""
        try:
            await workflow.wait_condition(condition, timeout=reminder_delta)
            return True
        except asyncio.TimeoutError:
            pass

        await self._audit(reminder_action)

        if remaining_delta.total_seconds() <= 0:
            return False

        try:
            await workflow.wait_condition(condition, timeout=remaining_delta)
            return True
        except asyncio.TimeoutError:
            return False

    async def _emit_clarification_episode(self, recurring: list[str], missing: list[str]) -> None:
        """Fase 4 (WSC-E4-T2) — grava UM skill_episode (source=clarification)
        quando a mesma lacuna de clarificacao recorre. Proveniencia completa
        (repo/campos/round/requester) para a esteira de promocao do WS-C
        governar. NENHUMA skill e criada aqui (fronteira testada em
        packages/contracts) — so o insumo. `pattern_key` agrupa ocorrencias do
        MESMO conjunto de campos recorrentes por tenant."""
        input = self._input
        pattern_key = "clarification_missing:" + "+".join(recurring)
        result = await workflow.execute_activity(
            LOCAL_ACTIVITY_RECORD_SKILL_EPISODE,
            {
                "tenant_id": input.tenant_id,
                "source": "clarification",
                "work_item_id": input.work_item_id,
                "pattern_key": pattern_key,
                "provenance": {
                    "repo": input.repo,
                    "recurring_missing": recurring,
                    "missing_now": list(missing),
                    "clarification_round": input.clarification_rounds,
                    "requester": input.requester,
                },
            },
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )
        await self._audit(
            "skill_episode_recorded",
            {"source": "clarification", "pattern_key": pattern_key,
             "recurring_missing": recurring,
             "occurrence_n": (result or {}).get("occurrence_n")},
        )

    # ------------------------------------------------------------------
    # Fase 2 — implementacao (WSB-E2-T3 estendida, WSB-E3-T2/T3, WSB-E4, WSB-E5-T1)
    # Sequencia: [budget admission] -> Planner (read-only) -> [gate de
    # aprovacao de plano] -> provision -> (Coder -> Tester -> L1)* -> L2 -> PR.
    # ------------------------------------------------------------------
    async def _run_implementation_phase(self) -> WorkItemLifecycleResult:
        input = self._input

        # WSB-E4-T1 — budget na admissao (nunca corta mid-Activity, so aqui).
        await self._audit(
            "budget_admitted",
            {"max_usd": input.budget_max_usd, "spent_usd": round(input.spent_usd, 6)},
        )
        await self._budget_boundary("admission")

        # WSB-E2-T3 estendida + WSB-E3-T2 — Planner read-only ANTES do gate, e o
        # gate de aprovacao. So roda enquanto nao ha sandbox (uma vez por
        # implementacao; re_plan reentra com sandbox_id None e plan_json vazio).
        if not input.sandbox_id:
            await self._run_planner_and_gate()

        if not input.sandbox_id:
            await self._boundary_gate()
            handle: SandboxHandle = await self._run_model_activity(
                ACTIVITY_PROVISION_SANDBOX,
                {
                    "work_item_id": input.work_item_id,
                    "tenant_id": input.tenant_id,
                    "repo": input.repo,
                    "base_branch": input.base_branch or "main",
                },
                SandboxHandle,
            )
            input.sandbox_id = handle.sandbox_id
            input.branch = handle.branch
            # S7: retem o handle COMPLETO (L1/finalize precisam dele, nao so do id).
            input.sandbox_handle = handle.model_dump()
            await self._set_status(WorkItemStatus.implementing, audit_action="sandbox_provisioned",
                                    details={"sandbox_id": handle.sandbox_id})
            # Estado visível durante a fase MAIS LONGA (coding): sem isto, uma
            # tarefa low-risk auto-aprovada some da superfície entre a admissão
            # e o pr_ready. O comentário único é editado in-place (sem spam).
            # (guard de replay vive dentro de _post_status_comment.)
            await self._post_status_comment("implementing")
            # Nova Activity protegida por patch marker: histories antigos
            # replayam sem o comando; execucoes novas persistem a base antes
            # do primeiro turno do Coder.
            if workflow.patched("capture-base-sha-before-coder-v1"):
                await self._capture_base_sha()

        while True:
            await self._boundary_gate()
            await self._budget_boundary("coder_turn")
            await self._maybe_retry_from_checkpoint()

            coder_result: CoderTurnResult = await self._run_model_activity(
                ACTIVITY_RUN_CODER_TURN,
                {
                    "sandbox_id": input.sandbox_id,
                    "work_item_id": input.work_item_id,
                    "tenant_id": input.tenant_id,
                    # S1: RunCoderTurnInput exige `instruction` (singular). Antes
                    # o workflow mandava `instructions`/`objections` (campos que
                    # o model nao tem — descartados no decode); agora a tarefa
                    # real + objecoes do L2 vao dobradas na instrucao.
                    "instruction": self._agent_instruction(include_objections=True),
                    "branch": input.branch,
                    "model_override": self._model_override,
                    "runtime_override": self._runtime_override,
                    # âncora do plano: arquivos novos fora dela são podados
                    # pós-turn (o CLI cria relatórios espontâneos — achado real)
                    "expected_files": list((input.plan_json or {}).get("expected_files", [])),
                },
                CoderTurnResult,
            )
            await self._consume_cost(coder_result.cost_usd, source="coder")
            await self._audit(
                "coder_turn_completed",
                {"files_changed": coder_result.files_changed, "cost_usd": coder_result.cost_usd},
            )

            # WSB-E2-T3 — Tester session (escreve/ajusta testes ANTES do L1).
            await self._boundary_gate()
            await self._budget_boundary("tester_turn")
            tester_result: TesterTurnResult = await self._run_model_activity(
                ACTIVITY_RUN_TESTER_TURN,
                {
                    "sandbox_id": input.sandbox_id,
                    "work_item_id": input.work_item_id,
                    "tenant_id": input.tenant_id,
                    "plan": input.plan_json,
                    "model_override": self._model_override,
                    "runtime_override": self._runtime_override,
                },
                TesterTurnResult,
            )
            await self._consume_cost(tester_result.cost_usd, source="tester")
            await self._audit(
                "tester_turn_completed",
                {
                    "files_changed": tester_result.files_changed,
                    "tests_ran": tester_result.tests_ran,
                    "tests_passed": tester_result.tests_passed,
                    "status": tester_result.status.value,
                },
            )

            if workflow.patched("enforce-tester-result-v1"):
                if not tester_result.tests_ran:
                    return await self._finish_failed(
                        "tester_contract_failed:tests_ran=false",
                        audit_action="tester_tests_not_run",
                    )
                if tester_result.status != GateStatus.PASS:
                    input.coder_retry_count += 1
                    if input.coder_retry_count > input.coder_retry_cap:
                        return await self._finish_failed(
                            "tester_failed_after_retry_cap",
                            audit_action="tester_retry_cap_exhausted",
                        )
                    await self._set_status(
                        WorkItemStatus.implementing,
                        audit_action="tester_failed_retrying",
                        details={"attempt": input.coder_retry_count},
                    )
                    continue

            await self._boundary_gate()
            await self._checkpoint_or_rebuild("implementing")

            await self._set_status(WorkItemStatus.validating, audit_action="l1_started")
            await self._boundary_gate()
            await self._budget_boundary("l1")
            l1_payload = {
                "sandbox": self._input.sandbox_handle,
                "plan": input.plan_json,
                "tenant_id": input.tenant_id,
                "base_branch": input.base_branch or "main",
            }
            if workflow.patched("sha-bound-validation-inputs-v1"):
                l1_payload.update({
                    "work_item_id": input.work_item_id,
                    "base_sha": input.base_sha or "",
                    "head_sha": input.head_sha or "",
                })
            l1_result: L1Result = await self._run_model_activity(
                ACTIVITY_RUN_L1_PIPELINE,
                l1_payload,
                L1Result,
            )
            await self._audit(
                "l1_completed",
                {"passed": l1_result.passed, "findings": [f.check for f in l1_result.findings]},
            )

            l1_passed = (
                l1_result.status == GateStatus.PASS
                if workflow.patched("structured-gate-status-v1")
                else l1_result.passed
            )
            if not l1_passed:
                # Achado do disparo real (queimou ~$8 em fix cycles inúteis):
                # manifesto L1 ausente/erro é problema de CONFIG DO REPO —
                # nenhum turno de Coder conserta isso. Escala com instrução
                # clara em vez de re-rodar o Coder (P6, custo).
                if workflow.patched("l1-manifest-escalates-v1"):
                    manifest_bad = next(
                        (f for f in l1_result.findings
                         if f.check == "l1_manifest" and not f.passed), None,
                    )
                    if manifest_bad is not None:
                        raise _EscalateNow(
                            "l1_manifest_not_configured: the target repo needs "
                            f".dse/validation.json on the base branch ({manifest_bad.detail[:160]})"
                        )
                input.coder_retry_count += 1
                if input.coder_retry_count > self._input.coder_retry_cap:
                    self._input.terminal_detail = (
                        f"l1_failed_after_{input.coder_retry_count - 1}_retries: "
                        + "; ".join(f"{f.check}:{f.detail}" for f in l1_result.findings if not f.passed)
                    )
                    await self._set_status(WorkItemStatus.failed, audit_action="coder_retry_cap_exhausted")
                    try:
                        await workflow.execute_activity(
                            ACTIVITY_TEARDOWN_SANDBOX,
                            {"work_item_id": input.work_item_id, "tenant_id": input.tenant_id,
                             "stage": "l1_retry_cap_exhausted"},
                            start_to_close_timeout=timedelta(seconds=120),
                            retry_policy=RetryPolicy(maximum_attempts=3),
                        )
                    except ActivityError:
                        logger.warning("teardown falhou apos exaustao de retries; seguindo mesmo assim")
                    return WorkItemLifecycleResult(
                        work_item_id=input.work_item_id,
                        status=WorkItemStatus.failed.value,
                        detail=self._input.terminal_detail,
                        pr_number=input.pr_number,
                    )
                await self._set_status(WorkItemStatus.implementing, audit_action="l1_failed_retrying",
                                        details={"attempt": input.coder_retry_count})
                continue  # novo turno do Coder no mesmo sandbox/branch

            # L1 passou -> Reviewer L2 de contexto fresco (WSC-E3-T5/WSE-E2).
            # P3: a sessao L2 recebe SO plan artifact + diff final — NUNCA o
            # historico/instrucoes do Coder (construido em `_run_l2_review`).
            await self._boundary_gate()
            await self._budget_boundary("l2")
            l2: L2Verdict = await self._run_l2_review(coder_result)
            await self._consume_cost(l2.cost_usd, source="l2")

            l2_passed = (
                l2.status == GateStatus.PASS
                if workflow.patched("structured-l2-gate-status-v1")
                else l2.passed
            )
            if not l2_passed:
                input.l2_retry_count += 1
                input.l2_objections = list(l2.objections)
                await self._audit(
                    "l2_objections_raised",
                    {"attempt": input.l2_retry_count, "objections": l2.objections},
                )
                if input.l2_retry_count > self._input.l2_retry_cap:
                    raise _EscalateNow(
                        f"l2_objections_after_retry_cap (objections={l2.objections})"
                    )
                await self._set_status(WorkItemStatus.implementing,
                                        audit_action="l2_changes_requested")
                continue  # volta ao Coder com as objecoes do L2

            input.l2_objections = []  # limpo — L2 aprovou
            break

        await self._boundary_gate()
        await self._budget_boundary("finalize_pr")
        finalize_payload = {
            "work_item_id": input.work_item_id,
            "tenant_id": input.tenant_id,
            "sandbox": self._input.sandbox_handle,
            "repo": input.repo,
            "base_branch": input.base_branch or "main",
            "branch": input.branch,
            "summary": self._pr_summary(),
            "issue_ref": ({"issue_number": input.issue_number}
                          if input.issue_number else None),
            "evidence_url": self._l1_evidence(l1_result),
        }
        if workflow.patched("sha-bound-finalize-input-v1"):
            finalize_payload.update({
                "base_sha": input.base_sha or "",
                "head_sha": input.head_sha or "",
                "risk_class": input.risk_class,
            })
        try:
            pr_ref: PrRef = await workflow.execute_activity(
            ACTIVITY_FINALIZE_PR,
            finalize_payload,
            result_type=PrRef,
            retry_policy=RetryPolicy(maximum_attempts=max(1, input.activity_retry_cap)),
            **self._activity_timeouts(),
            )
        except ActivityError as exc:
            raise _ActivityRetriesExhausted(
                ACTIVITY_FINALIZE_PR, str(exc.cause or exc)[:300]
            )
        input.pr_number = pr_ref.pr_number
        input.pr_url = pr_ref.url
        input.head_sha = pr_ref.head_sha or input.head_sha
        # Fase 4 — marca o instante de PR finalizado (leitura deterministica/
        # replay-safe de workflow.now()) para o "tempo ate merge" do pilot gate.
        if input.pr_finalized_at_epoch is None:
            input.pr_finalized_at_epoch = workflow.now().timestamp()

        await self._boundary_gate()
        await workflow.execute_activity(
            ACTIVITY_POST_TRACKING_COMMENT,
            {
                "work_item_id": input.work_item_id,
                "tenant_id": input.tenant_id,
                "pr_number": pr_ref.pr_number,
                "status": ("pr_open" if workflow.patched("fine-pr-ci-states-v1") else "pr_ready"),
            },
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )

        await self._checkpoint_or_rebuild("pr_open")
        pr_open_status = (
            WorkItemStatus.pr_open
            if workflow.patched("fine-pr-open-state-v1")
            else WorkItemStatus.pr_ready
        )
        await self._set_status(pr_open_status, audit_action="pr_finalized",
                                details={"pr_number": pr_ref.pr_number, "url": pr_ref.url})
        # PR aberto -> coluna de review no board (In Review). Sem isto o card
        # ficava preso em In Progress mesmo com o PR pronto (achado BD-39).
        await self._post_board_transition(pr_open_status.value)

        # Fase 3 — pipeline de evidencia INICIAL (depois de finalize_pr):
        # trigger_preview (paths-filter deterministico com o files_changed do
        # CoderTurnResult, FR-20) -> se created, run_demo_evidence (base_url do
        # preview; o publish acontece DENTRO dela, WS-E) -> run_visual_diff.
        # Falha/degradacao NUNCA bloqueia o PR (failure mode 9) — evidencia
        # degradada e registrada e o fluxo segue para review humano.
        input.last_files_changed = list(coder_result.files_changed)
        await self._run_evidence_pipeline(coder_result.files_changed, reason="initial")
        await self._emit_history_metric("pr_finalized")

        # NAO fazemos `continue_as_new` aqui (deliberado — ver README.md,
        # secao "continue_as_new e a corrida de sinais"): um humano/CI pode
        # reagir ao status `pr_ready` (que acabou de ficar visivel via query)
        # tao rapido que o sinal chegaria exatamente durante a janela em que
        # o run antigo esta fechando via continue_as_new — e um sinal
        # endereçado a um run que esta fechando pode ser perdido (nao
        # entregue ao novo run). Continuar na MESMA execucao elimina essa
        # corrida por construcao: nao ha run "fechando" para perder o sinal.
        input.phase = PHASE_REVIEW
        return await self._run_review_phase()

    # ------------------------------------------------------------------
    # WSB-E3-T2 — Planner read-only + gate de aprovacao de plano por risk class
    # ------------------------------------------------------------------
    async def _run_planner_and_gate(self) -> None:
        """Roda o Planner (read-only) e aplica o gate. Ao retornar, o plano
        esta aprovado (auto por politica OU por humano nomeado) e o workflow
        pode provisionar/implementar. Levanta `_PlanRejected`/`_BlockNow`/
        `_EscalateNow`/`_CancelledByOperator` nos caminhos que nao seguem."""
        input = self._input

        await self._boundary_gate()
        await self._budget_boundary("planner")
        plan: PlanArtifact = await self._run_model_activity(
            ACTIVITY_RUN_PLANNER_TURN,
            {
                "work_item_id": input.work_item_id,
                "tenant_id": input.tenant_id,
                "repo": input.repo,
                "base_branch": input.base_branch,
                # S1: instrucao real (conteudo da issue + aceite + esclarecimentos).
                "instruction": self._agent_instruction(),
                "model_override": self._model_override,
            },
            PlanArtifact,
        )
        input.plan_json = plan.model_dump()
        input.plan_rounds += 1
        await self._audit(
            "planner_completed",
            {"risk_class_declared": plan.risk_class, "expected_files": plan.expected_files,
             "round": input.plan_rounds},
        )

        # P1: classificacao de risco EFETIVA por POLITICA deterministica (fora
        # do modelo) — um Planner que sub-classifica NAO rebaixa o gate.
        effective_risk = policy.classify_risk(plan.expected_files, plan.risk_class)
        input.risk_class = effective_risk

        if workflow.patched("reject-empty-expected-files-v1"):
            if not plan.expected_files and not plan.no_code_change:
                await self._audit(
                    "planner_contract_rejected",
                    {"reason": "expected_files_empty", "round": input.plan_rounds},
                )
                raise _EscalateNow("planner_expected_files_empty_without_no_code_change")

        # Postgres recebe plano/risco antes do Coder. O hash e a lista de
        # expected_files sao derivados pela Activity server-side.
        if workflow.patched("persist-plan-before-coder-v1"):
            await self._persist_status()

        if not policy.requires_plan_approval(effective_risk, input.require_approval_risk_classes):
            # Risco baixo -> auto-aprova POR POLITICA (nunca por ausencia de
            # aprovador). Projeta e audita.
            await self._record_gate(status="approved", auto_approved=True,
                                    approvers=[], decided_by=None)
            await self._audit("plan_auto_approved", {"risk_class": effective_risk})
            return

        # Risco alto -> resolve aprovador pela cascata CODEOWNERS -> access bundle.
        resolved = await workflow.execute_activity(
            LOCAL_ACTIVITY_RESOLVE_APPROVER,
            {"tenant_id": input.tenant_id, "repo": input.repo,
             "work_item_id": input.work_item_id, "channel": None},
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )
        approvers = list(resolved.get("approvers") or [])
        input.approvers = approvers

        if not approvers:
            # CASCATA VAZIA -> Blocked + escalacao, JAMAIS auto-aprova (P1/P3).
            await self._record_gate(status="blocked", auto_approved=False,
                                    approvers=[], decided_by=None)
            await self._audit("plan_gate_no_approver_blocked", {"risk_class": effective_risk})
            raise _BlockNow(
                f"plan_approval_cascade_empty:no CODEOWNERS/designated approver for tenant={input.tenant_id}"
            )

        # Estaciona em espera DURAVEL (novo estado awaiting_plan_approval).
        # ORDEM (corrigida na integração da Fase 3): grava a projeção do gate
        # ANTES de flipar o status para awaiting_plan_approval. Invariante: o
        # gate durável (consumido pelo queue board e pelo roteamento de signal
        # do WS-A, que dispara SIGNAL_PLAN_APPROVAL com base no status) tem que
        # existir no instante em que o estado se torna observável — senão um
        # observador que vê o status pode ler o gate ainda ausente (corrida
        # real reproduzida por teste; a edição da Fase 3 do review loop mudou
        # o timing e expôs a inversão).
        await self._record_gate(status="pending", auto_approved=False,
                                approvers=approvers, decided_by=None)
        await self._set_raw_status(
            STATUS_AWAITING_PLAN_APPROVAL,
            audit_action="awaiting_plan_approval",
            details={"risk_class": effective_risk, "approvers": approvers,
                     "source": resolved.get("source")},
        )
        # Renderiza o pedido de aprovacao via adapters (WS-A edita a mensagem
        # de status unica in-place; aqui so pedimos que ela reflita o gate).
        try:
            await workflow.execute_activity(
                ACTIVITY_POST_TRACKING_COMMENT,
                {"work_item_id": input.work_item_id, "tenant_id": input.tenant_id,
                 "pr_number": None, "status": "awaiting_plan_approval"},
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
        except ActivityError:
            logger.warning("post_tracking_comment do gate falhou; gate segue durável mesmo assim")

        # Espera durável pelo SIGNAL_PLAN_APPROVAL (roteado pelo WS-A quando
        # status==awaiting_plan_approval). Anti-clobber: nao resetamos a flag
        # antes de ler o payload.
        await workflow.wait_condition(
            lambda: self._plan_approval_received or self._cancelled
            or self._operator_escalate_requested
        )
        if self._cancelled:
            raise _CancelledByOperator()
        if self._operator_escalate_requested:
            raise _EscalateNow(self._operator_escalate_reason or "operator_escalate")

        payload = self._plan_approval_payload or {}
        self._plan_approval_received = False  # consumido
        verdict = payload.get("verdict")
        actor = payload.get("actor") or payload.get("decided_by") or "unknown"

        if verdict == "approved":
            await self._record_gate(status="approved", auto_approved=False,
                                    approvers=approvers, decided_by=actor)
            await self._audit("plan_approved", {"decided_by": actor, "risk_class": effective_risk})
            input.status = WorkItemStatus.queued.value
            await self._persist_status()
            return

        if verdict == "rejected":
            route = payload.get("route")
            justification = payload.get("justification") or payload.get("comment") or ""
            # Rejeicao SEMPRE auditada com identidade + justificativa (WSB-E3-T3).
            await self._record_gate(status="rejected", auto_approved=False, approvers=approvers,
                                    decided_by=actor, route=route, justification=justification)
            await self._audit(
                "plan_rejected",
                {"decided_by": actor, "route": route, "justification": justification,
                 "risk_class": effective_risk},
            )
            if route not in ("re_plan", "re_clarify", "cancel"):
                raise _EscalateNow(f"plan_rejected_unknown_route:{route!r}")
            raise _PlanRejected(route=route, actor=actor, justification=justification)

        raise _EscalateNow(f"unknown_plan_verdict:{verdict!r}")

    async def _record_gate(self, *, status: str, auto_approved: bool, approvers: list[str],
                           decided_by: str | None, route: str | None = None,
                           justification: str | None = None) -> None:
        """Projecao duravel do gate (migracao 0009) — consultavel pelo queue
        board/operadores. NAO substitui o audit ledger (o proprio metodo caller
        tambem chama `_audit`)."""
        await workflow.execute_activity(
            LOCAL_ACTIVITY_RECORD_GATE,
            {
                "work_item_id": self._input.work_item_id,
                "tenant_id": self._input.tenant_id,
                "risk_class": self._input.risk_class,
                "status": status,
                "auto_approved": auto_approved,
                "resolved_approvers": approvers,
                "decided_by": decided_by,
                "rejection_route": route,
                "justification": justification,
                "plan_round": self._input.plan_rounds,
            },
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )

    async def _run_l2_review(self, coder_result: CoderTurnResult) -> "L2Verdict":
        """WSC-E3-T5/WSE-E2 — sessao Reviewer L2 de contexto fresco. P3
        (NAO-NEGOCIAVEL): o payload contem SO o plan artifact + o diff final;
        NADA do historico/instrucoes do Coder (nenhum `instructions`,
        `clarification_notes`, `objections` — deliberadamente ausentes)."""
        input = self._input
        payload = {
            "work_item_id": input.work_item_id,
            "tenant_id": input.tenant_id,
            "plan": input.plan_json,
            "diff": coder_result.diff_summary,
        }
        if workflow.patched("sha-bound-l2-input-v1"):
            payload.update({"base_sha": input.base_sha, "head_sha": input.head_sha})
        l2: L2Verdict = await self._run_model_activity(
            ACTIVITY_RUN_L2_REVIEW,
            payload,
            L2Verdict,
        )
        await self._audit("l2_review_completed",
                          {"passed": l2.passed, "status": l2.status.value,
                           "objections": l2.objections,
                           "base_sha": input.base_sha, "head_sha": input.head_sha})
        return l2

    # ------------------------------------------------------------------
    # WSB-E3-T3 — rejection path: 3 rotas deterministicas. Nenhuma dispara
    # implementacao sem passar de novo pelo gate correspondente.
    # ------------------------------------------------------------------
    async def _handle_plan_rejection(self, exc: "_PlanRejected") -> WorkItemLifecycleResult:
        input = self._input
        if exc.route == "cancel":
            detail = f"plan_rejected_cancel: {exc.justification}"
            input.terminal_detail = detail
            await self._set_status(
                WorkItemStatus.failed, audit_action="plan_rejected_cancelled",
                details={"decided_by": exc.actor, "justification": exc.justification},
            )
            return WorkItemLifecycleResult(
                work_item_id=input.work_item_id, status=WorkItemStatus.failed.value,
                detail=detail, pr_number=input.pr_number,
            )

        if exc.route == "re_clarify":
            # Volta ao gate de CLARIFICACAO (nao a implementacao): reentra o
            # intake do zero — implementacao so recomeca apos clarificacao +
            # planner + gate de novo. Reabrir a clarificacao significa exigir
            # que o requester re-forneca os criterios de aceite (o aprovador
            # rejeitou justamente por ambiguidade do escopo): limpamos
            # `acceptance_criteria` para o checklist deterministico reabrir a
            # rodada (nunca "adivinha" — P1/P6).
            input.phase = PHASE_INTAKE
            input.status = WorkItemStatus.needs_clarification.value
            input.clarification_rounds = 0
            input.acceptance_criteria = None
            input.plan_json = {}
            input.risk_class = "low"
            input.approvers = []
            await self._audit("plan_rejected_route_re_clarify", {"decided_by": exc.actor})
            await self._persist_status()
            return await self._continue_as_new(input)

        if exc.route == "re_plan":
            # Re-planeja: limpa o plano e reentra a fase de implementacao, que
            # comeca pelo Planner + gate de novo (nunca pula o gate). Capado.
            if input.plan_rounds >= input.plan_round_cap:
                return await self._finish_escalated(
                    f"plan_round_cap_exhausted:{input.plan_rounds}"
                )
            input.plan_json = {}
            input.approvers = []
            input.status = WorkItemStatus.queued.value
            await self._audit("plan_rejected_route_re_plan",
                              {"decided_by": exc.actor, "round": input.plan_rounds})
            await self._persist_status()
            return await self._run_implementation_phase()

        return await self._finish_escalated(f"plan_rejected_unknown_route:{exc.route}")

    # ------------------------------------------------------------------
    # Fase 3 — review humano (WSB-E3-T4). NENHUM path chama merge da API do
    # GitHub aqui — so espera `merged_by_human` (P3: nenhuma sessao de agente
    # aprova/mergeia o proprio trabalho).
    # ------------------------------------------------------------------
    def _drain_review_comments(self) -> list[dict[str, Any]]:
        """Consome o LOTE inteiro de comentarios pendentes. Anti-clobber: so e
        chamado DEPOIS de `_review_received` ficar True; o handler do sinal
        apenas acrescenta a lista, entao nada se perde entre o drain e a
        proxima espera."""
        comments = list(self._review_comments)
        self._review_comments.clear()
        self._review_received = False
        return comments

    def _bump_review_round(self) -> None:
        """WSB-E4-T2 — cap explicito de rounds de review (o loop de review e o
        unico `while` do workflow que ainda nao tinha cap proprio). Esgotado ->
        escalated (nunca um loop infinito por construcao)."""
        self._input.review_round += 1
        if self._input.review_round > self._input.review_round_cap:
            raise _EscalateNow(
                f"review_round_cap_exhausted:{self._input.review_round - 1}"
            )

    async def _update_base_branch_before_review_fix(self) -> None:
        """Fase 4 (WSE-E6-T16, wiring WS-B) — no caminho changes_requested,
        ANTES de re-rodar o Coder: atualiza o branch da tarefa com o drift da
        base SEM reescrever historia.

        `first_human_review_done=True` aqui e nao-negociavel: chegamos a este
        ponto justamente PORQUE um humano ja revisou o PR e pediu mudancas —
        depois do 1o review a estrategia so pode ser merge-base-into-branch. Um
        rebase+force-push orfanaria as threads de review ancoradas nos commits
        reescritos (comportamento verificado do GitHub, failure mode 11). A
        estrategia e deterministica (P1: codigo no WS-E, nao modelo). Conflito
        nao-resolvivel -> escala a humano (NUNCA resolve a forca)."""
        input = self._input
        if not (input.repo and input.branch and input.base_branch):
            # Modo estrito/sem branch conhecido: nada a mesclar. Declina limpo.
            await self._audit("base_branch_update_skipped_no_branch", {})
            return
        await self._boundary_gate()
        result: UpdateBaseBranchResult = await workflow.execute_activity(
            ACTIVITY_UPDATE_BASE_BRANCH,
            {
                "work_item_id": input.work_item_id,
                "tenant_id": input.tenant_id,
                "repo": input.repo,
                "branch": input.branch,
                "base_branch": input.base_branch,
                "first_human_review_done": True,
            },
            result_type=UpdateBaseBranchResult,
            **self._activity_timeouts(),
        )
        await self._audit(
            "base_branch_updated",
            {"strategy": result.strategy, "conflict": result.conflict,
             "orphaned_threads": result.orphaned_threads, "detail": result.detail},
        )
        if result.conflict:
            # P6: nunca resolve a forca; escala com a identidade do drift.
            raise _EscalateNow(
                f"base_branch_merge_conflict:repo={input.repo} branch={input.branch} "
                f"base={input.base_branch} ({result.detail})"
            )
        if result.orphaned_threads:
            # Invariante de exit da Fase 4: merge-base NUNCA orfana threads
            # (rebase pos-review e proibido por construcao). Se o dono (WS-E)
            # reportou >0, algo violou a garantia — nao seguimos adivinhando (P6).
            raise _EscalateNow(
                f"base_branch_orphaned_threads:{result.orphaned_threads} "
                f"(strategy={result.strategy}) — violates the zero-orphaned-threads invariant"
            )

    async def _emit_pr_quality_metric(self, outcome: str) -> None:
        """Fase 4 — emite as metricas de qualidade de PR (pilot gate "PR quality
        thresholds"). Leitura deterministica no workflow (contadores + tempo via
        workflow.now); emissao OTel na Activity local. Best-effort: falha de
        metrica jamais afeta o fluxo. Chamada nas fronteiras terminais do PR
        (merge/escalacao)."""
        input = self._input
        time_to_merge = None
        if outcome == "merged" and input.pr_finalized_at_epoch is not None:
            time_to_merge = max(0.0, workflow.now().timestamp() - input.pr_finalized_at_epoch)
        payload = {
            "work_item_id": input.work_item_id,
            "tenant_id": input.tenant_id,
            "outcome": outcome,
            "review_rounds": input.review_round,
            "changes_requested_count": input.changes_requested_count,
            "evidence_refreshes": input.evidence_refreshes,
            "time_to_merge_seconds": time_to_merge,
        }
        try:
            await workflow.execute_activity(
                LOCAL_ACTIVITY_EMIT_PR_QUALITY_METRIC,
                payload,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError:
            logger.warning("emit_pr_quality_metric falhou; metrica e best-effort, fluxo segue")

    def _validate_merge_signal(self) -> tuple[bool, str, MergedByHumanSignal | None]:
        """Valida o envelope ja autenticado/correlacionado pelo adapter.

        A verificacao remota do estado do PR continua pertencendo ao adapter/
        finalizador; aqui impedimos que um signal vazio, de outro PR/repo/SHA
        ou sem identidade humana conclua o workflow.
        """
        try:
            merge = MergedByHumanSignal(**(self._merge_payload or {}))
        except Exception as exc:  # payload de signal e dado nao confiavel
            return False, f"invalid_payload:{type(exc).__name__}", None
        if self._input.pr_number is None or merge.pr_number != self._input.pr_number:
            return False, "pr_number_mismatch", merge
        if merge.repo and self._input.repo and merge.repo != self._input.repo:
            return False, "repo_mismatch", merge
        if merge.head_sha and self._input.head_sha and merge.head_sha != self._input.head_sha:
            return False, "head_sha_mismatch", merge
        return True, "ok", merge

    async def _verify_merge_via_github_api(self, merge) -> tuple[bool, str]:
        """Plano 08 §F (F1) — confirma o merge contra a API do GitHub (verdade),
        não só o envelope. Retorna (ok_para_concluir, motivo).

        Política:
          - PR de fato merged (e head_sha bate) → (True, "api_verified").
          - refutação DEFINITIVA (PR não existe / não está merged / sha diverge)
            → (False, motivo): forte sinal de forja; NÃO conclui (P1).
          - API indisponível (erro de rede/credencial) → (True, "api_unavailable"):
            degrada para o envelope, que já passou por assinatura HMAC do webhook
            + correlação (defesa em profundidade; não trava merge por outage).
        Guard de replay: histories antigas não chamavam esta Activity."""
        if not workflow.patched("merge-verify-github-api-v1"):
            return True, "unpatched"
        if not self._input.pr_number or not self._input.repo:
            return True, "no_target"
        try:
            v: MergeVerification = await workflow.execute_activity(
                ACTIVITY_VERIFY_MERGE_STATE,
                {"work_item_id": self._input.work_item_id, "tenant_id": self._input.tenant_id,
                 "repo": self._input.repo, "pr_number": self._input.pr_number,
                 "expected_head_sha": self._input.head_sha},
                result_type=MergeVerification,
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError as exc:
            # Activity caiu inteira após retries: degrada para o envelope
            # autenticado (não trava merge legítimo por indisponibilidade).
            logger.warning("verify_merge_state indisponível; degradando p/ envelope: %s", exc)
            return True, "api_unavailable_activity_error"
        if v.verified:
            return True, f"api_verified(merged_by={v.merged_by})"
        # refutação definitiva vs indisponibilidade (fail-safe distinto)
        if v.reason.startswith(("not_merged", "pr_not_found", "head_sha_mismatch")):
            return False, v.reason
        return True, f"api_unavailable:{v.reason}"

    async def _run_review_phase(self) -> WorkItemLifecycleResult:
        """Loop `while True` (NAO `continue_as_new` por iteracao — ver
        comentario em `_run_implementation_phase` sobre a corrida de sinais):
        cada volta reconsulta CI, espera veredito humano, e se for
        `changes_requested` (ou CI red) aplica o ciclo de fix no MESMO
        branch/PR e volta ao topo, tudo na MESMA execucao de workflow.
        Fase 3 (WSB-E4-T2): rounds capados por `review_round_cap`; comentarios
        consumidos em LOTE com janela de debounce (ADR-26) — N comentarios numa
        janela viram UM ciclo de fix + UM refresh de evidencia."""
        input = self._input

        while True:
            await self._boundary_gate()
            # Ativacao do alerta de history: o loop de review e onde o history
            # cresce sem Continue-As-New (limitacao documentada) — emite a
            # metrica a cada volta para o collector/WS-F.
            await self._emit_history_metric("review_loop")
            fine_states = workflow.patched("ci-pending-blocks-review-v1")
            if fine_states:
                input.ci_status = "pending"
                await self._set_status(
                    WorkItemStatus.ci_pending,
                    audit_action="ci_pending",
                    details={"head_sha": input.head_sha},
                )
            ci_payload = {
                "work_item_id": input.work_item_id,
                "tenant_id": input.tenant_id,
                "repo": input.repo,
                "ref": (input.head_sha or input.branch or "") if fine_states else (input.branch or ""),
                "pr_number": input.pr_number,
            }
            if fine_states:
                ci_payload.update({
                    "base_sha": input.base_sha or "",
                    "head_sha": input.head_sha or "",
                })
            try:
                ci: CiStatusResult = await workflow.execute_activity(
                    ACTIVITY_CONSUME_CI_STATUS,
                    ci_payload,
                    result_type=CiStatusResult,
                    retry_policy=RetryPolicy(maximum_attempts=max(1, input.activity_retry_cap)),
                    **self._activity_timeouts(),
                )
            except ActivityError as exc:
                raise _ActivityRetriesExhausted(
                    ACTIVITY_CONSUME_CI_STATUS, str(exc.cause or exc)[:300]
                )
            input.ci_status = ci.status
            await self._audit("ci_status_observed", {"status": ci.status})

            if fine_states and ci.status == "pending":
                input.ci_pending_polls += 1
                await self._persist_status()
                if input.ci_pending_polls >= input.ci_pending_poll_cap:
                    raise _EscalateNow(
                        f"ci_pending_poll_cap_exhausted:{input.ci_pending_polls}"
                    )
                await workflow.sleep(timedelta(seconds=max(0.01, input.ci_poll_interval_seconds)))
                continue

            if ci.status == "red":
                # CI vermelho antes de acordar um humano: volta ao Coder no
                # MESMO branch/PR (mesma disciplina de retry cap).
                input.coder_retry_count += 1
                if input.coder_retry_count > self._input.coder_retry_cap:
                    raise _EscalateNow("ci_red_after_retry_cap_exhausted")
                self._bump_review_round()
                await self._set_status(WorkItemStatus.review_feedback, audit_action="ci_red_retrying")
                fix_result = await self._apply_coder_fix_cycle(["ci red: corrigir o pipeline"])
                input.ci_pending_polls = 0
                # ADR-26: fix cycle = commit novo que muda comportamento -> 1 refresh
                input.last_files_changed = list(fix_result.files_changed)
                await self._run_evidence_pipeline(fix_result.files_changed,
                                                  reason="fix_cycle_ci_red")
                continue

            if fine_states and ci.status != "green":
                raise _EscalateNow(f"unknown_ci_status:{ci.status!r}")

            # IMPORTANTE: nao resetamos `_review_received` aqui antes de
            # esperar — um `review_comment` pode ja ter chegado (via signal
            # callback, que roda assim que o event loop do workflow cede o
            # controle) enquanto ainda estavamos na Activity de CI acima.
            # Resetar aqui apagaria essa resposta legitima e o workflow
            # ficaria esperando para sempre por um sinal que ja veio. So
            # consumimos e resetamos DEPOIS de drenar o lote, abaixo.
            ready_status = WorkItemStatus.review_ready if fine_states else WorkItemStatus.pr_ready
            await self._set_status(ready_status, audit_action="awaiting_human_review",
                                   details={"ci_status": ci.status, "head_sha": input.head_sha})
            await workflow.wait_condition(
                lambda: self._review_received or self._refresh_evidence_requested
                or self._cancelled or self._operator_escalate_requested
            )
            if self._cancelled:
                raise _CancelledByOperator()
            if self._operator_escalate_requested:
                raise _EscalateNow(self._operator_escalate_reason or "operator_escalate")

            if self._refresh_evidence_requested and not self._review_received:
                # ADR-26 — pedido humano explicito: o UNICO gatilho de refresh
                # sem commit novo. Sem files novos, reusa o ultimo conjunto
                # conhecido para o paths-filter deterministico (FR-20).
                # Consome o pedido AQUI (nao so dentro do pipeline): um refresh
                # DECLINADO pelo cap tambem conta como atendido — senao a flag
                # pendente viraria um hot-loop nesta espera.
                self._refresh_evidence_requested = False
                await self._audit("evidence_refresh_requested_by_human", {})
                await self._run_evidence_pipeline(list(input.last_files_changed),
                                                  reason="human_request")
                continue

            comments = self._drain_review_comments()

            # ADR-26 — debounce: se o lote pede mudancas e ha janela
            # configurada, espera a janela para agrupar comentarios que ainda
            # estao chegando — 6 comentarios numa janela = 1 fix + 1 refresh.
            # Decisao 100% deterministica: contagem/comparacao + timer durauel
            # do Temporal; nenhum LLM decide nada aqui (P1).
            if (input.evidence_debounce_seconds > 0
                    and any(c.get("verdict") == "changes_requested" for c in comments)):
                try:
                    await workflow.wait_condition(
                        lambda: self._cancelled or self._operator_escalate_requested,
                        timeout=timedelta(seconds=input.evidence_debounce_seconds),
                    )
                except asyncio.TimeoutError:
                    pass
                if self._cancelled:
                    raise _CancelledByOperator()
                if self._operator_escalate_requested:
                    raise _EscalateNow(self._operator_escalate_reason or "operator_escalate")
                late = self._drain_review_comments()
                comments.extend(late)
                await self._audit(
                    "review_comments_debounced",
                    {"count": len(comments), "window_s": input.evidence_debounce_seconds},
                )

            verdicts = [c.get("verdict") for c in comments]

            if "changes_requested" in verdicts:
                self._bump_review_round()
                input.changes_requested_count += 1
                texts = [str(c.get("comment") or "") for c in comments
                         if c.get("verdict") == "changes_requested"]
                await self._set_status(WorkItemStatus.review_feedback, audit_action="changes_requested",
                                        details={"comments": texts, "batched": len(comments)})
                # Fase 4 (WSE-E6-T16) — merge-base ANTES de re-rodar o Coder:
                # o humano ja revisou (first_human_review_done=True), entao o
                # drift da base entra por merge-base-into-branch, nunca rebase
                # (preserva as threads de review). Conflito -> escala.
                await self._update_base_branch_before_review_fix()
                fix_result = await self._apply_coder_fix_cycle(texts)
                # ADR-26: 1 lote de comentarios -> 1 fix cycle -> no maximo 1
                # refresh de evidencia (o refresh e disparado pelo COMMIT do
                # fix, nunca pelos comentarios em si).
                input.last_files_changed = list(fix_result.files_changed)
                await self._run_evidence_pipeline(fix_result.files_changed, reason="fix_cycle")
                continue

            if "approved" in verdicts:
                merge_status = (
                    WorkItemStatus.merge_pending if fine_states else WorkItemStatus.pr_ready
                )
                await self._set_status(merge_status, audit_action="approved_awaiting_merge")
                # (sem reset de `_merged` aqui pela mesma razao acima — comeca
                # False no __init__ e so este ramo o consome, uma vez por run)
                require_verified_signal = workflow.patched("validate-merge-signal-v1")
                while True:
                    await workflow.wait_condition(lambda: self._merged or self._cancelled)
                    if self._cancelled:
                        raise _CancelledByOperator()
                    if not require_verified_signal:
                        break
                    valid, reason, merge = self._validate_merge_signal()
                    if valid:
                        # Plano 08 §F (F1): o envelope (pr_number/repo/sha) não é
                        # segredo — um webhook forjado com os campos certos
                        # passaria o check acima. Confirma na API do GitHub que o
                        # PR está REALMENTE merged antes de concluir (P1/P8).
                        api_ok, api_reason = await self._verify_merge_via_github_api(merge)
                        if api_ok:
                            await self._audit(
                                "merge_signal_verified",
                                {"merged_by": merge.merged_by,
                                 "pr_number": merge.pr_number,
                                 "merge_sha": merge.merge_sha,
                                 "head_sha": input.head_sha,
                                 "api_verification": api_reason},
                            )
                            break
                        reason = f"api_refuted:{api_reason}"
                    await self._audit(
                        "merge_signal_rejected",
                        {"reason": reason, "payload": dict(self._merge_payload or {})},
                    )
                    self._merged = False
                    self._merge_payload = None
                await self._checkpoint_or_rebuild("done")
                if input.sandbox_id:
                    try:
                        await workflow.execute_activity(
                            ACTIVITY_TEARDOWN_SANDBOX,
                            {"work_item_id": input.work_item_id, "tenant_id": input.tenant_id,
                             "stage": "done"},
                            start_to_close_timeout=timedelta(seconds=120),
                            retry_policy=RetryPolicy(maximum_attempts=3),
                        )
                    except ActivityError:
                        logger.warning("teardown falhou apos merge; seguindo mesmo assim (nao bloqueia Done)")
                await self._set_status(
                    WorkItemStatus.done,
                    audit_action="merged_by_human",
                    details={"merged_by": (self._merge_payload or {}).get("merged_by"),
                             "pr_number": input.pr_number},
                )
                # Merge humano -> coluna Done no board (fecha o ciclo do card).
                await self._post_board_transition(WorkItemStatus.done.value)
                # Fase 4 — metrica de qualidade de PR na fronteira de merge
                # (pilot gate). Best-effort, apos o Done ja estar persistido.
                await self._emit_pr_quality_metric("merged")
                return WorkItemLifecycleResult(
                    work_item_id=input.work_item_id, status=WorkItemStatus.done.value,
                    pr_number=input.pr_number,
                )

            # veredito desconhecido: nunca adivinha (P6) — escala.
            raise _EscalateNow(f"unknown_review_verdict:{verdicts!r}")

    async def _apply_coder_fix_cycle(self, comments: list[str]) -> CoderTurnResult:
        """`changes_requested` (humano, possivelmente um LOTE debounced) ou CI
        red: volta ao Coder no MESMO branch/PR, re-valida L1, re-finaliza o
        MESMO PR (idempotente). Retorna o resultado do Coder — o caller usa
        `files_changed` para o refresh de evidencia (paths-filter FR-20)."""
        input = self._input
        await self._boundary_gate()
        await self._budget_boundary("review_fix_coder")
        await self._maybe_retry_from_checkpoint()

        review_notes = "\n".join(f"- {c.strip()}" for c in comments if c and c.strip())
        fix_instruction = self._agent_instruction(include_objections=True)
        if review_notes:
            fix_instruction += "\n\nComentários de review a resolver:\n" + review_notes
        coder_result: CoderTurnResult = await self._run_model_activity(
            ACTIVITY_RUN_CODER_TURN,
            {
                # S7: RunCoderTurnInput usa `instruction` (str) + `branch` — o
                # call site antigo mandava `instructions` (lista, campo inexistente).
                "sandbox_id": input.sandbox_id,
                "work_item_id": input.work_item_id,
                "tenant_id": input.tenant_id,
                "branch": input.branch,
                "instruction": fix_instruction,
                "model_override": self._model_override,
                "runtime_override": self._runtime_override,
                "expected_files": list((input.plan_json or {}).get("expected_files", [])),
            },
            CoderTurnResult,
        )
        await self._consume_cost(coder_result.cost_usd, source="coder_fix")
        await self._audit("coder_fix_applied", {"files_changed": coder_result.files_changed})

        await self._boundary_gate()
        await self._checkpoint_or_rebuild("review_feedback")

        await self._set_status(WorkItemStatus.validating, audit_action="l1_revalidation_started")
        await self._budget_boundary("review_fix_l1")
        l1_payload = {
            "sandbox": self._input.sandbox_handle,
            "plan": input.plan_json,
            "tenant_id": input.tenant_id,
            "base_branch": input.base_branch or "main",
        }
        if workflow.patched("sha-bound-review-l1-input-v1"):
            l1_payload.update({
                "work_item_id": input.work_item_id,
                "base_sha": input.base_sha or "",
                "head_sha": input.head_sha or "",
            })
        l1_result: L1Result = await self._run_model_activity(
            ACTIVITY_RUN_L1_PIPELINE, l1_payload, L1Result,
        )
        l1_passed = (
            l1_result.status == GateStatus.PASS
            if workflow.patched("structured-review-l1-status-v1")
            else l1_result.passed
        )
        if not l1_passed:
            input.coder_retry_count += 1
            if input.coder_retry_count > self._input.coder_retry_cap:
                raise _EscalateNow("l1_revalidation_failed_after_retry_cap")
            # tenta mais uma vez recursivamente ate o cap (mantem no mesmo branch/PR)
            return await self._apply_coder_fix_cycle(comments)

        await self._boundary_gate()
        finalize_payload = {
            "work_item_id": input.work_item_id,
            "tenant_id": input.tenant_id,
            "sandbox": self._input.sandbox_handle,
            "repo": input.repo,
            "base_branch": input.base_branch or "main",
            "branch": input.branch,
            "summary": self._pr_summary(),
            "issue_ref": ({"issue_number": input.issue_number}
                          if input.issue_number else None),
            "evidence_url": self._l1_evidence(l1_result),
        }
        if workflow.patched("sha-bound-refinalize-input-v1"):
            finalize_payload.update({
                "base_sha": input.base_sha or "",
                "head_sha": input.head_sha or "",
                "risk_class": input.risk_class,
            })
        try:
            pr_ref: PrRef = await workflow.execute_activity(
                ACTIVITY_FINALIZE_PR,
                finalize_payload,
                result_type=PrRef,
                retry_policy=RetryPolicy(maximum_attempts=max(1, input.activity_retry_cap)),
                **self._activity_timeouts(),
            )
        except ActivityError as exc:
            raise _ActivityRetriesExhausted(
                ACTIVITY_FINALIZE_PR, str(exc.cause or exc)[:300]
            )
        input.pr_number = pr_ref.pr_number
        input.pr_url = pr_ref.url
        input.head_sha = pr_ref.head_sha or input.head_sha
        await workflow.execute_activity(
            ACTIVITY_POST_TRACKING_COMMENT,
            {
                "work_item_id": input.work_item_id,
                "tenant_id": input.tenant_id,
                "pr_number": pr_ref.pr_number,
                "status": "pr_updated",
            },
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )
        input.ci_status = None
        refinalized_status = (
            WorkItemStatus.pr_open
            if workflow.patched("refinalize-resets-ci-v1")
            else WorkItemStatus.pr_ready
        )
        await self._set_status(
            refinalized_status,
            audit_action="pr_refinalized",
            details={"pr_number": pr_ref.pr_number, "head_sha": input.head_sha},
            clear_ci_status=True,
        )
        return coder_result

    # ------------------------------------------------------------------
    # Fase 3 — pipeline de evidencia (WS-E implementa as Activities; aqui so
    # a orquestracao deterministica, pelos NOMES/models de dse_contracts).
    # trigger_preview -> (created) run_demo_evidence -> run_visual_diff.
    # O publish do video/trace acontece DENTRO de run_demo_evidence (WS-E).
    # Failure mode 9: preview/demo degradado NUNCA bloqueia o PR — evidencia
    # degradada e registrada (audit + projecao 0014) e o fluxo segue para
    # review humano.
    # ------------------------------------------------------------------
    async def _run_evidence_pipeline(self, files_changed: list[str], *, reason: str) -> None:
        input = self._input
        if input.pr_number is None:
            # Modo estrito (compare_url sem PR): sem pr_number nao ha preview
            # por PR — declina limpo e auditado (P6), nunca bloqueia.
            await self._audit("evidence_skipped_no_pr", {"reason": reason})
            return
        if reason != "initial":
            if input.evidence_refreshes >= input.evidence_refresh_cap:
                # ADR-26: cap de refreshes — declina LIMPO (auditado); a
                # evidencia fica stale, o PR nao e bloqueado (P6).
                await self._audit(
                    "evidence_refresh_declined_cap",
                    {"reason": reason, "refreshes": input.evidence_refreshes,
                     "cap": input.evidence_refresh_cap},
                )
                return
            input.evidence_refreshes += 1
        # Qualquer pedido humano pendente e atendido por ESTE refresh (nunca
        # empilha um segundo refresh para o mesmo estado do branch).
        self._refresh_evidence_requested = False

        # Plano 08 §D — gate deploys_preview (operator-set no painel Repos & ROI).
        # Guard de replay: histories antigas nao chamavam esta local activity e
        # devem replayar com preview_enabled=True (comportamento anterior).
        preview_enabled = True
        if workflow.patched("preview-gate-deploys-v1"):
            try:
                gate = await workflow.execute_activity(
                    LOCAL_ACTIVITY_PREVIEW_ENABLED,
                    {"tenant_id": input.tenant_id, "repo": input.repo},
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
                preview_enabled = bool(gate.get("enabled", True))
            except ActivityError:
                logger.warning("preview_enabled_for_repo falhou; fail-open (preview nunca bloqueia)")

        try:
            preview: PreviewRef = await workflow.execute_activity(
                ACTIVITY_TRIGGER_PREVIEW,
                {
                    "work_item_id": input.work_item_id,
                    "tenant_id": input.tenant_id,
                    "repo": input.repo,
                    "pr_number": input.pr_number,
                    "files_changed": list(files_changed),
                    "preview_enabled": preview_enabled,
                },
                result_type=PreviewRef,
                start_to_close_timeout=timedelta(seconds=900),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError as exc:
            input.preview_status = "degraded"
            await self._audit(
                "evidence_degraded",
                {"stage": "trigger_preview", "reason": reason,
                 "error": str(exc.cause or exc)[:300]},
            )
            await self._record_evidence(reason=reason, detail="trigger_preview_failed")
            return

        input.preview_status = preview.status
        input.preview_url = preview.url
        await self._audit(
            "preview_triggered",
            {"status": preview.status, "url": preview.url,
             "namespace": preview.namespace, "reason": reason},
        )

        if preview.status in ("skipped_backend_only", "skipped_disabled"):
            # Decisao deterministica de nao-preview (paths-filter FR-20 ou gate
            # deploys_preview §D) — conta como SUCESSO, nunca bloqueia.
            await self._audit(
                "evidence_skipped_backend_only",
                {"reason": reason, "preview_status": preview.status},
            )
            await self._record_evidence(reason=reason, detail=preview.status)
            return
        if preview.status != "created":
            # "degraded" ou qualquer status desconhecido: degrada limpo (P6),
            # nunca bloqueia nem adivinha.
            await self._audit(
                "evidence_degraded",
                {"stage": "trigger_preview", "reason": reason,
                 "status": preview.status, "detail": (preview.detail or "")[:300]},
            )
            await self._record_evidence(reason=reason, detail=(preview.detail or "")[:300])
            return

        # Plano 08 §D (D1) — o objetivo do usuario: o LINK do preview aparece no
        # PR para o humano acessar e decidir. Postado assim que o preview existe
        # (independe de demo/visual, que degradam sem bloquear). Best-effort e
        # guardado por patch (histories antigas nao postavam) — nunca bloqueia.
        if preview.url and workflow.patched("preview-link-in-pr-v1"):
            await self._post_preview_link(preview.url, preview.kind)

        try:
            demo: DemoEvidenceResult = await workflow.execute_activity(
                ACTIVITY_RUN_DEMO_EVIDENCE,
                {
                    "work_item_id": input.work_item_id,
                    "tenant_id": input.tenant_id,
                    "base_url": preview.url,
                },
                result_type=DemoEvidenceResult,
                start_to_close_timeout=timedelta(seconds=900),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError as exc:
            await self._audit(
                "evidence_degraded",
                {"stage": "run_demo_evidence", "reason": reason,
                 "error": str(exc.cause or exc)[:300]},
            )
            await self._record_evidence(reason=reason, detail="demo_evidence_failed")
            return

        input.evidence_passed = demo.passed
        input.evidence_video_key = demo.video_artifact_key
        input.evidence_trace_key = demo.trace_artifact_key
        await self._audit(
            "demo_evidence_completed",
            {"passed": demo.passed, "video_artifact_key": demo.video_artifact_key,
             "trace_artifact_key": demo.trace_artifact_key,
             "duration_s": demo.duration_s, "reason": reason},
        )

        # Visual diff quando ha screenshot. Gap de contrato documentado no
        # README: DemoEvidenceResult nao carrega uma chave de screenshot, entao
        # o gatilho deterministico e "a demo produziu midia" (video/trace) e o
        # candidato segue a convencao demos/<work_item_id>/ (ADR-27, WSC-E3-T4b).
        if demo.video_artifact_key or demo.trace_artifact_key:
            try:
                vd: VisualDiffResult = await workflow.execute_activity(
                    ACTIVITY_RUN_VISUAL_DIFF,
                    {
                        "work_item_id": input.work_item_id,
                        "tenant_id": input.tenant_id,
                        "base_screenshot_key": input.visual_baseline_key,
                        "candidate_screenshot_path": f"demos/{input.work_item_id}/screenshot.png",
                    },
                    result_type=VisualDiffResult,
                    start_to_close_timeout=timedelta(seconds=300),
                    retry_policy=RetryPolicy(maximum_attempts=2),
                )
                if vd.baseline_created and vd.diff_artifact_key:
                    # 1o run: WS-E publica o candidato como baseline e retorna a
                    # chave em diff_artifact_key (assuncao documentada no README).
                    input.visual_baseline_key = vd.diff_artifact_key
                await self._audit(
                    "visual_diff_completed",
                    {"passed": vd.passed, "changed_pct": vd.changed_pct,
                     "baseline_created": vd.baseline_created, "reason": reason},
                )
            except ActivityError as exc:
                await self._audit(
                    "evidence_degraded",
                    {"stage": "run_visual_diff", "reason": reason,
                     "error": str(exc.cause or exc)[:300]},
                )
        await self._record_evidence(reason=reason, detail="ok")

    async def _post_preview_link(self, url: str, kind: str) -> None:
        """Plano 08 §D (D1) — posta/edita o comentário de tracking da superfície
        de origem com o LINK clicável do preview. Reusa a MutableCommentWriter
        (C3) via post_tracking_comment (body custom). Best-effort — jamais
        bloqueia (o audit ledger é a verdade; o comentário é conveniência)."""
        input = self._input
        label = "frontend (UI)" if kind == "ui" else "service" if kind == "deployable" else "app"
        body = (
            f"🔗 **Preview ({label}) ready** — open it and decide:\n\n{url}\n\n"
            "_Ephemeral per-PR environment (Argo CD, TTL). Merging remains a "
            "human decision (P1)._"
        )
        try:
            await workflow.execute_activity(
                ACTIVITY_POST_TRACKING_COMMENT,
                {"work_item_id": input.work_item_id, "tenant_id": input.tenant_id,
                 "status": "pr_ready", "body": body},
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
            await self._audit("preview_link_posted", {"url": url, "kind": kind})
        except ActivityError:
            logger.warning("post_preview_link best-effort falhou (url=%s)", url)

    async def _record_evidence(self, *, reason: str, detail: str) -> None:
        """Projecao duravel do estado de evidencia (migracao 0014) — best-effort:
        a projecao nunca bloqueia o fluxo (o audit ledger e a fonte imutavel)."""
        input = self._input
        try:
            await workflow.execute_activity(
                LOCAL_ACTIVITY_RECORD_EVIDENCE,
                {
                    "work_item_id": input.work_item_id,
                    "tenant_id": input.tenant_id,
                    "preview_status": input.preview_status,
                    "preview_url": input.preview_url,
                    "demo_passed": input.evidence_passed,
                    "video_artifact_key": input.evidence_video_key,
                    "trace_artifact_key": input.evidence_trace_key,
                    "visual_baseline_key": input.visual_baseline_key,
                    "refresh_count": input.evidence_refreshes,
                    "last_refresh_reason": reason,
                    "detail": detail,
                },
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError:
            logger.warning("record_evidence_state falhou; projecao e best-effort, fluxo segue")
