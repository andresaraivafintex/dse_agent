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
        ACTIVITY_RUN_L1_PIPELINE,
        ACTIVITY_TEARDOWN_SANDBOX,
        CiStatusResult,
        CoderTurnResult,
        L1Result,
        PrRef,
        SandboxHandle,
    )
    from dse_contracts.constants import WORKFLOW_TYPE
    from dse_contracts.work_item import WorkItemStatus

    from dse_orchestrator.local_activities import (
        LOCAL_ACTIVITY_CHECK_CLARIFICATION,
        LOCAL_ACTIVITY_LOAD_WORK_ITEM,
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


class _CancelledByOperator(Exception):
    pass


class _ForceClarification(Exception):
    def __init__(self, reason: str):
        self.reason = reason


class _EscalateNow(Exception):
    def __init__(self, reason: str):
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
        self._merged = False

    # ------------------------------------------------------------------
    # Signals — WSB-E2-T4, WSB-E3-T1, WSB-E3-T4, WSB-E5-T2
    # ------------------------------------------------------------------
    @workflow.signal
    def clarification_answer(self, payload: dict[str, Any]) -> None:
        self._clarification_payload = payload
        self._clarification_received = True

    @workflow.signal
    def review_comment(self, payload: dict[str, Any]) -> None:
        self._review_payload = payload
        self._review_received = True

    @workflow.signal
    def merged_by_human(self, payload: dict[str, Any] | None = None) -> None:
        self._merged = True

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
        except _ForceClarification as exc:
            input.phase = PHASE_INTAKE
            input.status = WorkItemStatus.needs_clarification.value
            input.clarification_rounds = 0
            input.terminal_detail = f"force_clarification: {exc.reason}"
            await self._audit("force_clarification_applied", {"reason": exc.reason})
            await self._persist_status()
            return workflow.continue_as_new(input)

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
            return WorkItemLifecycleInput(
                work_item_id=row["work_item_id"],
                tenant_id=row["tenant_id"],
                requester=row["requester"],
                repo=row.get("repo"),
                base_branch=row.get("base_branch"),
                data_class=row.get("data_class") or "internal",
                pr_number=row.get("pr_number"),
            )
        raise ApplicationError(
            f"WorkItemLifecycleWorkflow.run recebeu um tipo de input nao suportado: {type(raw_input)!r}"
        )

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

    async def _persist_status(self) -> None:
        payload = {
            "work_item_id": self._input.work_item_id,
            "status": self._input.status,
            "pr_number": self._input.pr_number,
        }
        await workflow.execute_activity(
            LOCAL_ACTIVITY_UPDATE_STATUS,
            payload,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )

    async def _set_status(self, status: WorkItemStatus, *, audit_action: str | None = None,
                           details: dict[str, Any] | None = None) -> None:
        self._input.status = status.value
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
                    {
                        "sandbox_id": self._input.sandbox_id,
                        "work_item_id": self._input.work_item_id,
                        "reason": "cancelled_by_operator",
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
        return WorkItemLifecycleResult(
            work_item_id=self._input.work_item_id,
            status=WorkItemStatus.failed.value,
            detail=detail,
            pr_number=self._input.pr_number,
        )

    async def _finish_escalated(self, reason: str) -> WorkItemLifecycleResult:
        detail = f"escalated: {reason}"
        self._input.terminal_detail = detail
        await self._set_status(
            WorkItemStatus.escalated,
            audit_action="escalated",
            details={"reason": reason},
        )
        return WorkItemLifecycleResult(
            work_item_id=self._input.work_item_id,
            status=WorkItemStatus.escalated.value,
            detail=detail,
            pr_number=self._input.pr_number,
        )

    async def _checkpoint_or_rebuild(self, phase_name: str) -> None:
        """WSB-E5-T1: checkpoint ao fim de cada fase, com retries limitados;
        esgotado, tenta rebuild; esgotado tambem, escala (nunca segue
        adivinhando com um sandbox potencialmente corrompido)."""
        if not self._input.sandbox_id:
            return
        for _ in range(self._input.checkpoint_retry_cap):
            try:
                await workflow.execute_activity(
                    ACTIVITY_CHECKPOINT_SANDBOX,
                    {
                        "sandbox_id": self._input.sandbox_id,
                        "work_item_id": self._input.work_item_id,
                        "phase": phase_name,
                    },
                    start_to_close_timeout=timedelta(seconds=120),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
                return
            except ActivityError:
                continue
        await self._audit("checkpoint_exhausted_attempting_rebuild", {"phase": phase_name})
        for _ in range(self._input.rebuild_retry_cap):
            try:
                handle: SandboxHandle = await workflow.execute_activity(
                    ACTIVITY_REBUILD_SANDBOX,
                    {"work_item_id": self._input.work_item_id, "sandbox_id": self._input.sandbox_id},
                    result_type=SandboxHandle,
                    start_to_close_timeout=timedelta(seconds=300),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
                self._input.sandbox_id = handle.sandbox_id
                await self._audit("rebuild_succeeded", {"phase": phase_name})
                return
            except ActivityError:
                continue
        raise _EscalateNow(f"checkpoint_and_rebuild_exhausted:{phase_name}")

    async def _maybe_retry_from_checkpoint(self) -> None:
        if not self._retry_from_checkpoint_requested:
            return
        self._retry_from_checkpoint_requested = False
        await self._audit("retry_from_checkpoint_applied", {})
        try:
            handle: SandboxHandle = await workflow.execute_activity(
                ACTIVITY_REBUILD_SANDBOX,
                {"work_item_id": self._input.work_item_id, "sandbox_id": self._input.sandbox_id},
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
                },
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
            if completeness["complete"]:
                await self._set_status(WorkItemStatus.ready, audit_action="clarification_complete")
                input.phase = PHASE_IMPLEMENTATION
                input.status = WorkItemStatus.queued.value
                return workflow.continue_as_new(input)

            if input.clarification_rounds >= self._input.clarification_round_cap:
                raise _EscalateNow(
                    f"clarification_round_cap_exhausted (missing={completeness['missing']})"
                )

            await self._set_status(
                WorkItemStatus.needs_clarification,
                audit_action="clarification_requested",
                details={"missing": completeness["missing"], "round": input.clarification_rounds},
            )

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

    # ------------------------------------------------------------------
    # Fase 2 — implementacao (WSB-E2-T3, WSB-E5-T1)
    # ------------------------------------------------------------------
    async def _run_implementation_phase(self) -> WorkItemLifecycleResult:
        input = self._input

        if not input.sandbox_id:
            await self._boundary_gate()
            handle: SandboxHandle = await workflow.execute_activity(
                ACTIVITY_PROVISION_SANDBOX,
                {
                    "work_item_id": input.work_item_id,
                    "tenant_id": input.tenant_id,
                    "repo": input.repo,
                    "base_branch": input.base_branch,
                },
                result_type=SandboxHandle,
                **self._activity_timeouts(),
            )
            input.sandbox_id = handle.sandbox_id
            input.branch = handle.branch
            await self._set_status(WorkItemStatus.implementing, audit_action="sandbox_provisioned",
                                    details={"sandbox_id": handle.sandbox_id})

        while True:
            await self._boundary_gate()
            await self._maybe_retry_from_checkpoint()

            coder_result: CoderTurnResult = await workflow.execute_activity(
                ACTIVITY_RUN_CODER_TURN,
                {
                    "sandbox_id": input.sandbox_id,
                    "work_item_id": input.work_item_id,
                    "tenant_id": input.tenant_id,
                    "instructions": list(input.clarification_notes),
                    "model_override": self._model_override,
                    "runtime_override": self._runtime_override,
                },
                result_type=CoderTurnResult,
                **self._activity_timeouts(),
            )
            await self._audit(
                "coder_turn_completed",
                {"files_changed": coder_result.files_changed, "cost_usd": coder_result.cost_usd},
            )

            await self._boundary_gate()
            await self._checkpoint_or_rebuild("implementing")

            await self._set_status(WorkItemStatus.validating, audit_action="l1_started")
            await self._boundary_gate()
            l1_result: L1Result = await workflow.execute_activity(
                ACTIVITY_RUN_L1_PIPELINE,
                {"work_item_id": input.work_item_id, "sandbox_id": input.sandbox_id},
                result_type=L1Result,
                **self._activity_timeouts(),
            )
            await self._audit(
                "l1_completed",
                {"passed": l1_result.passed, "findings": [f.check for f in l1_result.findings]},
            )

            if l1_result.passed:
                break

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
                        {"sandbox_id": input.sandbox_id, "work_item_id": input.work_item_id,
                         "reason": "l1_retry_cap_exhausted"},
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
            # volta ao topo do loop: novo turno do Coder no mesmo sandbox/branch

        await self._boundary_gate()
        pr_ref: PrRef = await workflow.execute_activity(
            ACTIVITY_FINALIZE_PR,
            {
                "work_item_id": input.work_item_id,
                "tenant_id": input.tenant_id,
                "sandbox_id": input.sandbox_id,
                "repo": input.repo,
                "base_branch": input.base_branch,
                "branch": input.branch,
            },
            result_type=PrRef,
            **self._activity_timeouts(),
        )
        input.pr_number = pr_ref.pr_number
        input.pr_url = pr_ref.url

        await self._boundary_gate()
        await workflow.execute_activity(
            ACTIVITY_POST_TRACKING_COMMENT,
            {
                "work_item_id": input.work_item_id,
                "tenant_id": input.tenant_id,
                "pr_number": pr_ref.pr_number,
                "status": "pr_ready",
            },
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )

        await self._checkpoint_or_rebuild("pr_ready")
        await self._set_status(WorkItemStatus.pr_ready, audit_action="pr_finalized",
                                details={"pr_number": pr_ref.pr_number, "url": pr_ref.url})

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
    # Fase 3 — review humano (WSB-E3-T4). NENHUM path chama merge da API do
    # GitHub aqui — so espera `merged_by_human` (P3: nenhuma sessao de agente
    # aprova/mergeia o proprio trabalho).
    # ------------------------------------------------------------------
    async def _run_review_phase(self) -> WorkItemLifecycleResult:
        """Loop `while True` (NAO `continue_as_new` por iteracao — ver
        comentario em `_run_implementation_phase` sobre a corrida de sinais):
        cada volta reconsulta CI, espera veredito humano, e se for
        `changes_requested` (ou CI red) aplica o ciclo de fix no MESMO
        branch/PR e volta ao topo, tudo na MESMA execucao de workflow."""
        input = self._input

        while True:
            await self._boundary_gate()
            ci: CiStatusResult = await workflow.execute_activity(
                ACTIVITY_CONSUME_CI_STATUS,
                {"work_item_id": input.work_item_id, "pr_number": input.pr_number},
                result_type=CiStatusResult,
                **self._activity_timeouts(),
            )
            await self._audit("ci_status_observed", {"status": ci.status})

            if ci.status == "red":
                # CI vermelho antes de acordar um humano: volta ao Coder no
                # MESMO branch/PR (mesma disciplina de retry cap).
                input.coder_retry_count += 1
                if input.coder_retry_count > self._input.coder_retry_cap:
                    raise _EscalateNow("ci_red_after_retry_cap_exhausted")
                await self._set_status(WorkItemStatus.review_feedback, audit_action="ci_red_retrying")
                await self._apply_coder_fix_cycle()
                input.review_round += 1
                continue

            # IMPORTANTE: nao resetamos `_review_received` aqui antes de
            # esperar — um `review_comment` pode ja ter chegado (via signal
            # callback, que roda assim que o event loop do workflow cede o
            # controle) enquanto ainda estavamos na Activity de CI acima.
            # Resetar aqui apagaria essa resposta legitima e o workflow
            # ficaria esperando para sempre por um sinal que ja veio. So
            # consumimos e resetamos DEPOIS de ler `verdict` abaixo.
            await self._set_status(WorkItemStatus.pr_ready, audit_action="awaiting_human_review")
            await workflow.wait_condition(
                lambda: self._review_received or self._cancelled or self._operator_escalate_requested
            )
            if self._cancelled:
                raise _CancelledByOperator()
            if self._operator_escalate_requested:
                raise _EscalateNow(self._operator_escalate_reason or "operator_escalate")

            verdict = (self._review_payload or {}).get("verdict")
            self._review_received = False  # consumido — proxima volta espera um sinal NOVO

            if verdict == "changes_requested":
                await self._set_status(WorkItemStatus.review_feedback, audit_action="changes_requested",
                                        details={"comment": (self._review_payload or {}).get("comment")})
                await self._apply_coder_fix_cycle()
                input.review_round += 1
                continue

            if verdict == "approved":
                await self._set_status(WorkItemStatus.pr_ready, audit_action="approved_awaiting_merge")
                # (sem reset de `_merged` aqui pela mesma razao acima — comeca
                # False no __init__ e so este ramo o consome, uma vez por run)
                await workflow.wait_condition(lambda: self._merged or self._cancelled)
                if self._cancelled:
                    raise _CancelledByOperator()
                await self._checkpoint_or_rebuild("done")
                if input.sandbox_id:
                    try:
                        await workflow.execute_activity(
                            ACTIVITY_TEARDOWN_SANDBOX,
                            {"sandbox_id": input.sandbox_id, "work_item_id": input.work_item_id,
                             "reason": "done"},
                            start_to_close_timeout=timedelta(seconds=120),
                            retry_policy=RetryPolicy(maximum_attempts=3),
                        )
                    except ActivityError:
                        logger.warning("teardown falhou apos merge; seguindo mesmo assim (nao bloqueia Done)")
                await self._set_status(WorkItemStatus.done, audit_action="merged_by_human")
                return WorkItemLifecycleResult(
                    work_item_id=input.work_item_id, status=WorkItemStatus.done.value,
                    pr_number=input.pr_number,
                )

            # veredito desconhecido: nunca adivinha (P6) — escala.
            raise _EscalateNow(f"unknown_review_verdict:{verdict!r}")

    async def _apply_coder_fix_cycle(self) -> None:
        """`changes_requested` (humano) ou CI red: volta ao Coder no MESMO
        branch/PR, re-valida L1, re-finaliza o MESMO PR (idempotente)."""
        input = self._input
        await self._boundary_gate()
        await self._maybe_retry_from_checkpoint()

        coder_result: CoderTurnResult = await workflow.execute_activity(
            ACTIVITY_RUN_CODER_TURN,
            {
                "sandbox_id": input.sandbox_id,
                "work_item_id": input.work_item_id,
                "tenant_id": input.tenant_id,
                "instructions": [(self._review_payload or {}).get("comment", "")],
                "model_override": self._model_override,
                "runtime_override": self._runtime_override,
            },
            result_type=CoderTurnResult,
            **self._activity_timeouts(),
        )
        await self._audit("coder_fix_applied", {"files_changed": coder_result.files_changed})

        await self._boundary_gate()
        await self._checkpoint_or_rebuild("review_feedback")

        await self._set_status(WorkItemStatus.validating, audit_action="l1_revalidation_started")
        l1_result: L1Result = await workflow.execute_activity(
            ACTIVITY_RUN_L1_PIPELINE,
            {"work_item_id": input.work_item_id, "sandbox_id": input.sandbox_id},
            result_type=L1Result,
            **self._activity_timeouts(),
        )
        if not l1_result.passed:
            input.coder_retry_count += 1
            if input.coder_retry_count > self._input.coder_retry_cap:
                raise _EscalateNow("l1_revalidation_failed_after_retry_cap")
            # tenta mais uma vez recursivamente ate o cap (mantem no mesmo branch/PR)
            await self._apply_coder_fix_cycle()
            return

        await self._boundary_gate()
        pr_ref: PrRef = await workflow.execute_activity(
            ACTIVITY_FINALIZE_PR,
            {
                "work_item_id": input.work_item_id,
                "tenant_id": input.tenant_id,
                "sandbox_id": input.sandbox_id,
                "repo": input.repo,
                "base_branch": input.base_branch,
                "branch": input.branch,
                "existing_pr_number": input.pr_number,
            },
            result_type=PrRef,
            **self._activity_timeouts(),
        )
        input.pr_number = pr_ref.pr_number
        input.pr_url = pr_ref.url
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
        await self._set_status(WorkItemStatus.pr_ready, audit_action="pr_refinalized",
                                details={"pr_number": pr_ref.pr_number})
