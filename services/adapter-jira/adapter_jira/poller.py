"""WSA-E5-T2 — poller de fallback (OBRIGATÓRIO: o webhook do Jira é
best-effort e pode ser descartado silenciosamente).

O poller varre periodicamente os issues recentemente atualizados de cada
projeto configurado e RECONCILIA cada um pela MESMA via idempotente do
webhook (`adapter_jira.ingest`) — como os `message_id`/`event_id` são
derivados do ESTADO do issue (ver `events.py`), webhook e poller convergem no
mesmo `event_id` e a segunda via a chegar deduplica. Nunca duplicam.

Janela de sobreposição (`grace_seconds`): o cursor de cada projeto recua
`grace_seconds` a cada rodada, de modo que a próxima varredura re-inclui a
borda da janela anterior — assim nenhuma atualização que caiu exatamente no
limite é perdida (o dedup por `event_id` absorve a sobreposição sem custo).

Limitação documentada de atribuição: o poller vê apenas o ESTADO atual do
issue, não o changelog, então NÃO sabe QUEM fez uma transição de status. Uma
aprovação reconstruída pelo poller (webhook descartado) é atribuída ao
principal de sistema `system:adapter-jira-poller`; quando o webhook NÃO foi
descartado (caminho normal), ele chega com o ator real e, por ser idempotente,
o registro do ator real prevalece se chegar primeiro. Para tarefas, a
atribuição é estável (o reporter do issue), sem essa limitação.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from dse_identity import resolve_principal
from ingest_gateway.db import get_connection

from . import events
from .backend import JiraClientLike
from .ingest import ingest_comment, ingest_status_approval, ingest_task_trigger

logger = logging.getLogger("adapter_jira.poller")

_POLLER_PRINCIPAL = "system:adapter-jira-poller"


class JiraPoller:
    def __init__(
        self,
        client: JiraClientLike,
        *,
        tenant_id: str,
        projects: list[str],
        trigger_label: str,
        approved_status: str,
        rejected_status: str,
        grace_seconds: int = 120,
        reconcile_comments: bool = True,
    ):
        self._client = client
        self._tenant_id = tenant_id
        self._projects = projects
        self._trigger_label = trigger_label
        self._approved_status = approved_status
        self._rejected_status = rejected_status
        self._grace = timedelta(seconds=grace_seconds)
        self._reconcile_comments = reconcile_comments

    def poll_once(self, *, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        reconciled = 0
        for project in self._projects:
            since = self._get_cursor(project)
            since_iso = since.strftime("%Y-%m-%d %H:%M") if since else None
            issues = self._client.search_updated(project, since_iso)
            for issue in issues:
                reconciled += self._reconcile_issue(issue, now=now)
            # Cursor recua a janela de graça para não "perder" atualizações
            # que ainda estavam dentro da janela nesta rodada.
            self._set_cursor(project, now - self._grace)
        return reconciled

    def _reconcile_issue(self, issue: dict, *, now: datetime) -> int:
        count = 0
        key = events.ticket_key(issue)

        if events.has_trigger_label(issue, self._trigger_label):
            reporter = (issue.get("fields", {}).get("reporter") or {})
            account_id = reporter.get("accountId") or _POLLER_PRINCIPAL
            principal = (
                resolve_principal("jira", account_id, reporter.get("displayName"))
                if reporter.get("accountId")
                else _POLLER_PRINCIPAL
            )
            ingest_task_trigger(
                issue,
                tenant_id=self._tenant_id,
                actor_account_id=account_id,
                resolved_principal=principal,
                display_name=reporter.get("displayName"),
            )
            count += 1

        status = events.issue_status_name(issue)
        if status in (self._approved_status, self._rejected_status):
            verdict = "approved" if status == self._approved_status else "rejected"
            ingest_status_approval(
                issue,
                tenant_id=self._tenant_id,
                target_status=status,
                verdict=verdict,
                route="re_plan" if verdict == "rejected" else None,
                actor_account_id=_POLLER_PRINCIPAL,
                resolved_principal=_POLLER_PRINCIPAL,
            )
            count += 1

        if self._reconcile_comments:
            for c in self._client.get_comments(key):
                author = c.get("author") or {}
                account_id = author.get("accountId") or _POLLER_PRINCIPAL
                principal = (
                    resolve_principal("jira", account_id, author.get("displayName"))
                    if author.get("accountId")
                    else _POLLER_PRINCIPAL
                )
                ingest_comment(
                    tenant_id=self._tenant_id,
                    key=key,
                    comment_id=str(c["id"]),
                    body=c.get("body", ""),
                    actor_account_id=account_id,
                    resolved_principal=principal,
                    display_name=author.get("displayName"),
                )
                count += 1
        return count

    # --- cursor persistido (jira_poll_state) ---
    def _get_cursor(self, project: str) -> datetime | None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT last_polled_at FROM jira_poll_state WHERE tenant_id = %s AND project_key = %s",
                    (self._tenant_id, project),
                )
                row = cur.fetchone()
                return row[0] if row and row[0] else None
        finally:
            conn.close()

    def _set_cursor(self, project: str, ts: datetime) -> None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO jira_poll_state (tenant_id, project_key, last_polled_at)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (tenant_id, project_key)
                    DO UPDATE SET last_polled_at = EXCLUDED.last_polled_at
                    """,
                    (self._tenant_id, project, ts),
                )
            conn.commit()
        finally:
            conn.close()
