"""WSF-E6-T2 — controles de operador do queue board, conectados a signals
Temporal + estado durável, CADA AÇÃO AUDITADA com a identidade do operador (P8).

`OperatorConsole` recebe um `SignalSender` (real ou fake) e um `operator`
(principal_id resolvido do console via SSO — nunca um platform_user_id bruto).
Todos os controles do enunciado:
  pause / resume / cancel / retry_from_checkpoint / reassign_model /
  reassign_runtime / force_clarification / escalate / quarantine / release /
  kill switches (global, tenant, channel, task).

Ordem de cada controle: (1) audita a INTENÇÃO do operador (`operator_action`,
com actor = operador), (2) envia o signal / grava o estado durável. Se o signal
falhar, a falha é propagada (fail-closed, P6) — mas o audit da intenção já ficou
registrado (a evidência de que o operador tentou nunca se perde).
"""
from __future__ import annotations

from dse_audit import emit

from .. import kill_switches
from .signals import (
    SIGNAL_CANCEL,
    SIGNAL_ESCALATE,
    SIGNAL_FORCE_CLARIFICATION,
    SIGNAL_PAUSE,
    SIGNAL_REASSIGN_MODEL,
    SIGNAL_REASSIGN_RUNTIME,
    SIGNAL_RESUME,
    SIGNAL_RETRY_FROM_CHECKPOINT,
    SignalSender,
)


class OperatorConsole:
    def __init__(self, sender: SignalSender, *, operator: str):
        if not operator or not (operator.startswith("usr_") or operator.startswith("system:")):
            raise ValueError(
                "operator deve ser um principal resolvido (usr_...) ou system:<component> — "
                "nunca um platform_user_id bruto (P8)"
            )
        self._sender = sender
        self._operator = operator

    # -- helper: audita a intenção do operador (sempre antes do efeito) --
    def _audit_action(self, action: str, work_item_id: str | None, tenant_id: str, details: dict) -> None:
        emit(
            actor=self._operator,
            action=f"operator_{action}",
            tenant_id=tenant_id,
            work_item_id=work_item_id,
            details={**details, "operator": self._operator},
        )

    def _signal(self, work_item_id: str, name: str, arg=None) -> None:
        # workflow_id == work_item_id (contrato da fundação)
        self._sender.signal(work_item_id, name, arg)

    # ------------------------------------------------------------------
    # Controles ligados a signals de workflow (escopo TASK)
    # ------------------------------------------------------------------
    def pause(self, work_item_id: str, tenant_id: str, *, reason: str | None = None) -> None:
        self._audit_action("pause", work_item_id, tenant_id, {"reason": reason})
        self._signal(work_item_id, SIGNAL_PAUSE, reason)

    def resume(self, work_item_id: str, tenant_id: str, *, reason: str | None = None) -> None:
        self._audit_action("resume", work_item_id, tenant_id, {"reason": reason})
        self._signal(work_item_id, SIGNAL_RESUME, reason)

    def cancel(self, work_item_id: str, tenant_id: str, *, reason: str | None = None) -> None:
        self._audit_action("cancel", work_item_id, tenant_id, {"reason": reason})
        self._signal(work_item_id, SIGNAL_CANCEL, reason)

    def retry_from_checkpoint(self, work_item_id: str, tenant_id: str, *, reason: str | None = None) -> None:
        self._audit_action("retry_from_checkpoint", work_item_id, tenant_id, {"reason": reason})
        self._signal(work_item_id, SIGNAL_RETRY_FROM_CHECKPOINT, reason)

    def force_clarification(self, work_item_id: str, tenant_id: str, *, reason: str | None = None) -> None:
        self._audit_action("force_clarification", work_item_id, tenant_id, {"reason": reason})
        self._signal(work_item_id, SIGNAL_FORCE_CLARIFICATION, reason)

    def escalate(self, work_item_id: str, tenant_id: str, *, reason: str | None = None) -> None:
        self._audit_action("escalate", work_item_id, tenant_id, {"reason": reason})
        self._signal(work_item_id, SIGNAL_ESCALATE, reason)

    def reassign_model(self, work_item_id: str, tenant_id: str, *, model: str) -> None:
        if not model:
            raise ValueError("reassign_model exige `model`")
        self._audit_action("reassign_model", work_item_id, tenant_id, {"model": model})
        self._signal(work_item_id, SIGNAL_REASSIGN_MODEL, model)

    def reassign_runtime(self, work_item_id: str, tenant_id: str, *, runtime: str) -> None:
        if not runtime:
            raise ValueError("reassign_runtime exige `runtime`")
        self._audit_action("reassign_runtime", work_item_id, tenant_id, {"runtime": runtime})
        self._signal(work_item_id, SIGNAL_REASSIGN_RUNTIME, runtime)

    # ------------------------------------------------------------------
    # Quarentena (escopo TASK, com estado durável + pause)
    # ------------------------------------------------------------------
    def quarantine(self, work_item_id: str, tenant_id: str, *, reason: str) -> None:
        """Grava a quarentena durável (sobrevive a restart) E pausa o workflow.
        A quarentena é registrada mesmo que o pause falhe (o flag durável é a
        garantia; o pause é best-effort para parar o trabalho em voo)."""
        if not reason:
            raise ValueError("quarantine exige `reason` (P8)")
        self._audit_action("quarantine", work_item_id, tenant_id, {"reason": reason})
        kill_switches.quarantine_work_item(work_item_id, tenant_id, reason=reason, actor=self._operator)
        self._signal(work_item_id, SIGNAL_PAUSE, f"quarantined: {reason}")

    def release_quarantine(self, work_item_id: str, tenant_id: str, *, resume: bool = True) -> None:
        self._audit_action("release_quarantine", work_item_id, tenant_id, {"resume": resume})
        kill_switches.release_quarantine(work_item_id, tenant_id, actor=self._operator)
        if resume:
            self._signal(work_item_id, SIGNAL_RESUME, "quarantine_released")

    # ------------------------------------------------------------------
    # Kill switches nos 4 escopos
    # ------------------------------------------------------------------
    def kill_switch_global(self, *, enabled: bool, reason: str | None) -> None:
        self._audit_action("kill_switch_global", None, "platform", {"enabled": enabled, "reason": reason})
        kill_switches.set_global_kill_switch(enabled=enabled, reason=reason, actor=self._operator)

    def kill_switch_tenant(self, tenant_id: str, *, enabled: bool, reason: str | None) -> None:
        self._audit_action("kill_switch_tenant", None, tenant_id, {"enabled": enabled, "reason": reason})
        kill_switches.set_tenant_kill_switch(tenant_id, enabled=enabled, reason=reason, actor=self._operator)

    def kill_switch_channel(self, tenant_id: str, channel: str, *, active: bool, reason: str | None) -> None:
        self._audit_action("kill_switch_channel", None, tenant_id, {"channel": channel, "active": active, "reason": reason})
        kill_switches.set_channel_kill_switch(tenant_id, channel, active=active, reason=reason, actor=self._operator)

    def kill_switch_task(self, work_item_id: str, tenant_id: str, *, reason: str | None = None) -> None:
        """Kill switch de escopo TASK = cancelar o workflow (fim limpo em
        fronteira, P6 — o cancel do WS-B faz teardown do sandbox e marca failed)."""
        self.cancel(work_item_id, tenant_id, reason=reason or "task_kill_switch")
