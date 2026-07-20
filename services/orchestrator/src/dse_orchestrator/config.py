"""Configuracao do orquestrador (WS-B) via env var — nunca hardcoded, para que
testes possam acelerar timers/caps sem editar codigo (WSB-E3-T1).

Todos os valores tem um default de producao razoavel; os testes de
`temporalio.testing` (time-skipping) passam valores curtos via
`WorkItemLifecycleInput`/dataclasses de config explicitos em vez de mutar
variaveis de ambiente no meio do processo (mais seguro para paralelismo).
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return float(raw)


def _csv_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return tuple(part.strip().lower() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class OrchestratorConfig:
    """Caps e timers configuraveis (Fase 1: sem fairness/budget — isso e WSB-E4/Fase 2)."""

    # WSB-E3-T1 — gate de clarificacao
    clarification_round_cap: int = 3
    clarification_reminder_hours: float = 24.0
    clarification_escalation_days: float = 3.0

    # WSB-E2-T3 — loop de fix apos L1 falhar
    coder_retry_cap: int = 3

    # WSB-E5-T1 — checkpoint/rebuild
    checkpoint_retry_cap: int = 2
    rebuild_retry_cap: int = 1

    # Fase 2 — WSB-E3-T2/T3 (gate de aprovacao de plano) + WSB-E2 (L2).
    # POLITICA de quais classes de risco exigem aprovacao humana (P1: vive
    # fora do modelo, aqui na config do operador). CSV em
    # DSE_REQUIRE_APPROVAL_RISK_CLASSES (default "high").
    require_approval_risk_classes: tuple[str, ...] = ("high",)
    plan_round_cap: int = 3   # re_plan capado (rejection path)
    l2_retry_cap: int = 2     # objecoes do L2 -> Coder, capadas

    # timeouts de activity (segundos) — generosos porque Coder/L1 podem ser lentos;
    # heartbeat permite deteccao de worker morto sem esperar o timeout inteiro.
    activity_start_to_close_seconds: float = 3600.0
    activity_heartbeat_seconds: float = 30.0
    activity_schedule_to_close_seconds: float = 7200.0

    @classmethod
    def from_env(cls) -> "OrchestratorConfig":
        return cls(
            clarification_round_cap=_int_env("DSE_CLARIFICATION_ROUND_CAP", 3),
            clarification_reminder_hours=_float_env("DSE_CLARIFICATION_REMINDER_HOURS", 24.0),
            clarification_escalation_days=_float_env("DSE_CLARIFICATION_ESCALATION_DAYS", 3.0),
            coder_retry_cap=_int_env("DSE_CODER_RETRY_CAP", 3),
            checkpoint_retry_cap=_int_env("DSE_CHECKPOINT_RETRY_CAP", 2),
            rebuild_retry_cap=_int_env("DSE_REBUILD_RETRY_CAP", 1),
            activity_start_to_close_seconds=_float_env("DSE_ACTIVITY_START_TO_CLOSE_SECONDS", 3600.0),
            activity_heartbeat_seconds=_float_env("DSE_ACTIVITY_HEARTBEAT_SECONDS", 30.0),
            activity_schedule_to_close_seconds=_float_env("DSE_ACTIVITY_SCHEDULE_TO_CLOSE_SECONDS", 7200.0),
            require_approval_risk_classes=_csv_env("DSE_REQUIRE_APPROVAL_RISK_CLASSES", ("high",)),
            plan_round_cap=_int_env("DSE_PLAN_ROUND_CAP", 3),
            l2_retry_cap=_int_env("DSE_L2_RETRY_CAP", 2),
        )


DEFAULT_CONFIG = OrchestratorConfig()


def apply_to_input(input, cfg: "OrchestratorConfig | None" = None):
    """Preenche os campos de cap/timer de um `WorkItemLifecycleInput` a
    partir de `cfg` (default: `OrchestratorConfig.from_env()`). Chamado por
    quem inicia o workflow (ex.: o dispatcher do WS-A, ou `worker.py`/scripts
    de exemplo deste servico) — nunca pelo proprio workflow (env-var nao e
    lida dentro do sandbox de workflow)."""
    cfg = cfg or OrchestratorConfig.from_env()
    input.clarification_round_cap = cfg.clarification_round_cap
    input.clarification_reminder_hours = cfg.clarification_reminder_hours
    input.clarification_escalation_days = cfg.clarification_escalation_days
    input.coder_retry_cap = cfg.coder_retry_cap
    input.checkpoint_retry_cap = cfg.checkpoint_retry_cap
    input.rebuild_retry_cap = cfg.rebuild_retry_cap
    input.activity_start_to_close_seconds = cfg.activity_start_to_close_seconds
    input.activity_heartbeat_seconds = cfg.activity_heartbeat_seconds
    input.activity_schedule_to_close_seconds = cfg.activity_schedule_to_close_seconds
    input.require_approval_risk_classes = cfg.require_approval_risk_classes
    input.plan_round_cap = cfg.plan_round_cap
    input.l2_retry_cap = cfg.l2_retry_cap
    return input
