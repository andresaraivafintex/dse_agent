"""Kill switch por (tenant, canal) — WSA-E1-T3. Consultado pelo gateway
ANTES de `admit_work_item`; evento de canal desligado não cria WorkItem nem
processa, e gera `dse_audit.emit(action="admission_blocked_kill_switch")`.

Granularidade mais fina que `tenant_config.kill_switch_enabled` (WS-F,
tenant inteiro) — checamos as duas: se o tenant inteiro estiver com
kill-switch ligado (tabela do WS-F), tratamos como canal também desligado.
Leitura de `tenant_config` é best-effort/opcional (import defensivo) porque
essa tabela é dona do WS-F e pode não existir em todo ambiente de teste.
"""
from __future__ import annotations

from psycopg2.extensions import connection as _Connection


def is_channel_killed(conn: _Connection, tenant_id: str, channel: str) -> tuple[bool, str | None]:
    """Retorna (killed, reason). `channel` é o identificador de canal — ex.:
    Slack channel id, ou `owner/repo` para GitHub."""
    with conn.cursor() as cur:
        # 1) tenant-wide kill switch (WS-F), se a tabela existir neste ambiente.
        try:
            cur.execute(
                "SELECT kill_switch_enabled, kill_switch_reason FROM tenant_config WHERE tenant_id = %s",
                (tenant_id,),
            )
            row = cur.fetchone()
            if row is not None and row[0]:
                return True, row[1] or "tenant_wide_kill_switch"
        except Exception:
            conn.rollback()  # tabela pode não existir neste ambiente de teste; segue para o canal

        # 2) kill switch por canal (WS-A, migrations/0002_wsa.sql).
        cur.execute(
            "SELECT active, reason FROM channel_kill_switches WHERE tenant_id = %s AND channel = %s",
            (tenant_id, channel),
        )
        row = cur.fetchone()
        if row is not None and row[0]:
            return True, row[1] or "channel_kill_switch"

    return False, None
