"""WS-A ingest-gateway: gateway transacional (outbox), dispatcher Temporal,
defesas de intake (assinatura, TOCTOU snapshot, sanitização), correlação
Path A/B e steering allowlist.

Reutilizado como biblioteca pelos adapters (services/adapter-slack,
services/adapter-github) — eles chamam `admit_work_item`/`correlate`
diretamente contra o mesmo Postgres, o que mantém os adapters 100%
stateless (nenhum estado vive no processo do adapter).
"""
from .db import get_connection
from .kill_switch import is_channel_killed
from .gateway import admit_work_item, record_signal_event, AdmissionBlocked
from .correlate import correlate, CorrelationResult
from .steering import is_authorized_to_steer
from .security import (
    verify_slack_signature,
    verify_github_signature,
    verify_jira_signature,
    verify_teams_signature,
    SignatureCheck,
)
from .sanitize import sanitize_content
from .tenant_binding import resolve_tenant, default_tenant, ResolvedTenant
from .repo_resolver import resolve_repo, parse_explicit_repo

__all__ = [
    "get_connection",
    "is_channel_killed",
    "admit_work_item",
    "record_signal_event",
    "AdmissionBlocked",
    "correlate",
    "CorrelationResult",
    "is_authorized_to_steer",
    "verify_slack_signature",
    "verify_github_signature",
    "verify_jira_signature",
    "verify_teams_signature",
    "SignatureCheck",
    "sanitize_content",
    "resolve_tenant",
    "resolve_repo",
    "parse_explicit_repo",
    "default_tenant",
    "ResolvedTenant",
]
