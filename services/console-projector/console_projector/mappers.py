"""Mapas de contrato fase1 -> console (Plano 06 §4.4/§4.5).

Estes mapas SÃO a interface entre os dois produtos — versionados e travados
por teste de contrato (test_mappers.py): se um enum mudar de lado, o CI quebra
aqui, não o painel em produção (lição da remediação).
"""
from __future__ import annotations

from typing import Any

# fase1 WorkItemStatus (17) -> console DseTaskStatus (11) + phase legível.
# Nunca inventa: todo status fase1 TEM entrada; um status novo sem entrada
# derruba o teste de contrato (não silencia).
STATUS_MAP: dict[str, tuple[str, str | None]] = {
    "new": ("new", None),
    "needs_clarification": ("needs_clarification", "clarification"),
    "ready": ("ready", None),
    "queued": ("queued", None),
    "awaiting_plan_approval": ("blocked", "plan approval"),
    "implementing": ("running", "coding"),
    "validating": ("running", "validating"),
    "pr_open": ("pr_ready", "pr open"),
    "ci_pending": ("pr_ready", "ci pending"),
    "review_ready": ("pr_ready", "review"),
    "merge_pending": ("pr_ready", "merge pending"),
    "pr_ready": ("pr_ready", "review"),
    "review_feedback": ("review_feedback", "fixing review"),
    "done": ("done", None),
    "blocked": ("blocked", "no approver"),
    "failed": ("failed", None),
    "escalated": ("blocked", "escalated"),
}

# audit_log.action -> console TimelineEvent.type. Ações fora do mapa viram
# `note` (nunca silenciar — princípio "nunca sem descrição de estado").
AUDIT_EVENT_MAP: dict[str, str] = {
    "work_item_admitted": "created",
    "clarification_requested": "clarification_requested",
    "clarification_reminder_sent": "clarification_requested",
    "clarification_complete": "clarification_answered",
    "planner_turn_completed": "plan",
    "planner_completed": "plan",
    "plan_auto_approved": "plan",
    "awaiting_plan_approval": "plan",
    "planner_contract_rejected": "error",
    "coder_turn_completed": "file_change",
    "coder_fix_applied": "file_change",
    "tester_turn_completed": "test_result",
    "l1_pipeline_run": "test_result",
    "l1_completed": "test_result",
    "ci_status_observed": "test_result",
    "l2_review_completed": "feedback",
    "changes_requested": "feedback",
    "review_decision_signaled": "feedback",
    "pr_finalized": "pr_opened",
    "pr_refinalized": "pr_opened",
    "pr_adopted": "pr_opened",
    "status_comment_upserted": "comment",
    "tracking_comment_posted": "comment",
    "merged_by_human": "status_changed",
    "escalated": "error",
    "cancelled_by_operator": "error",
    "coder_retry_cap_exhausted": "error",
    "activity_retries_exhausted": "error",
    "model_path_fail_closed": "error",
    "budget_exhausted": "error",
}

_DETAIL_KEYS = ("reason", "pr_number", "url", "cost_usd", "status", "risk_class", "passed")


def map_status(fase1_status: str) -> tuple[str, str | None]:
    """(status_console, current_phase). KeyError proposital para status novo
    sem mapa — o projector loga e mantém a última projeção (fail-visible)."""
    return STATUS_MAP[fase1_status]


def map_audit_event(action: str, details: dict[str, Any] | None) -> tuple[str, str]:
    """(event_type, message legível). Determinístico; nada de LLM (P1)."""
    ev_type = AUDIT_EVENT_MAP.get(action, "note")
    details = details or {}
    extras = ", ".join(f"{k}={details[k]}" for k in _DETAIL_KEYS if details.get(k) not in (None, ""))
    message = action.replace("_", " ")
    if extras:
        message = f"{message} ({extras})"
    return ev_type, message


def split_title(content: str, fallback: str) -> tuple[str, str]:
    """Título = primeira linha não-vazia (≤120 chars); descrição = resto."""
    lines = [ln for ln in (content or "").strip().splitlines()]
    first = next((ln.strip() for ln in lines if ln.strip()), "")
    if not first:
        return fallback, ""
    rest = "\n".join(lines[1:]).strip()
    return first[:120], rest
