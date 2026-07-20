"""Entrypoint do poller de fallback (`python -m adapter_jira.poller_main`).
Processo separado — varre os projetos configurados e reconcilia via a mesma
via idempotente do webhook (WSA-E5-T2)."""
from __future__ import annotations

import os
import time

from ingest_gateway import get_connection, resolve_tenant

from . import config
from .backend import build_real_jira_client
from .poller import JiraPoller


def main() -> None:  # pragma: no cover - loop de produção
    interval = float(os.environ.get("JIRA_POLL_INTERVAL_SECONDS", "60"))
    conn = get_connection()
    try:
        tenant = resolve_tenant(conn, platform="jira", binding_key=config.get_base_url() or None)
        conn.commit()
    finally:
        conn.close()

    poller = JiraPoller(
        build_real_jira_client(),
        tenant_id=tenant.tenant_id,
        projects=config.get_poll_projects(),
        trigger_label=config.get_trigger_label(),
        approved_status=config.get_plan_approved_status(),
        rejected_status=config.get_plan_rejected_status(),
    )
    print(f"[adapter-jira] poller rodando (projetos={config.get_poll_projects()}, intervalo={interval}s)")
    while True:
        poller.poll_once()
        time.sleep(interval)


if __name__ == "__main__":  # pragma: no cover
    main()
