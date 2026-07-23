"""Evento normalizado único (§10.2, FR-01). Todo adapter (Slack/GitHub/Jira)
produz exclusivamente isto — nenhum código downstream do intake conhece o
formato nativo de nenhuma plataforma.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, field_validator


class Platform(str, Enum):
    slack = "slack"
    github = "github"
    jira = "jira"


class EventKind(str, Enum):
    task_request = "task_request"
    clarification_answer = "clarification_answer"
    approval = "approval"
    review_comment = "review_comment"
    steering = "steering"


class Actor(BaseModel):
    """Identidade de quem disparou o evento — bruta e (quando disponível) resolvida."""

    platform_user_id: str
    resolved_principal: str | None = None
    display_name: str | None = None


class ConversationEvent(BaseModel):
    """Contrato normalizado consumido pelo ingest-gateway e por todo o resto do sistema.

    `event_id` é a chave de idempotência de nível de evento (não confundir com
    `WorkItem.idempotency_key`, que é de nível de tarefa) — dois webhooks
    retry do mesmo `platform+thread+message` devem colidir em `event_id`.
    """

    model_config = {"frozen": True, "extra": "forbid"}

    event_id: str
    platform: Platform
    kind: EventKind
    source_ref: dict[str, Any]  # {thread_ts, channel} | {repo, issue_number} | {ticket_key}
    actor: Actor
    content_snapshot: str  # conteúdo congelado no momento da menção (defesa TOCTOU, FR-03)
    received_at: datetime
    signature_verified: bool

    @field_validator("received_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v

    @staticmethod
    def compute_event_id(platform: str, thread_key: str, message_id: str) -> str:
        raw = f"{platform}:{thread_key}:{message_id}".encode()
        return hashlib.sha256(raw).hexdigest()

    @classmethod
    def build(
        cls,
        *,
        platform: Platform,
        thread_key: str,
        message_id: str,
        kind: EventKind,
        source_ref: dict[str, Any],
        actor: Actor,
        content_snapshot: str,
        signature_verified: bool,
        received_at: datetime | None = None,
    ) -> "ConversationEvent":
        """Constrói o evento derivando `event_id` deterministicamente — nunca
        gere `event_id` à mão fora deste helper (garante a defesa de dedup)."""
        return cls(
            event_id=cls.compute_event_id(platform.value, thread_key, message_id),
            platform=platform,
            kind=kind,
            source_ref=source_ref,
            actor=actor,
            content_snapshot=content_snapshot,
            signature_verified=signature_verified,
            received_at=received_at or datetime.now(timezone.utc),
        )
