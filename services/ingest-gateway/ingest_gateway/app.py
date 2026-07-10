"""FastAPI da porta 8803 (WS-A, ver CONVENTIONS.md). O trabalho pesado do
ingest-gateway (`admit_work_item`, `correlate`, dispatcher) é biblioteca
consumida diretamente pelos adapters e pelo processo dispatcher — este app
expõe apenas health-check e administração operacional do kill switch de
canal (liga/desliga sem precisar de acesso direto ao Postgres).
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .db import get_connection

app = FastAPI(title="dse-ingest-gateway")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "ingest-gateway"}


class KillSwitchUpdate(BaseModel):
    tenant_id: str
    channel: str
    active: bool
    reason: str | None = None
    actor: str  # principal resolvido de quem está operando o kill switch (P8)


@app.put("/admin/kill-switch")
def set_kill_switch(body: KillSwitchUpdate) -> dict:
    from dse_audit import emit as audit_emit

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO channel_kill_switches (tenant_id, channel, active, reason)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (tenant_id, channel)
                DO UPDATE SET active = EXCLUDED.active, reason = EXCLUDED.reason
                """,
                (body.tenant_id, body.channel, body.active, body.reason),
            )
        audit_emit(
            actor=body.actor,
            action="channel_kill_switch_toggled",
            tenant_id=body.tenant_id,
            details={"channel": body.channel, "active": body.active, "reason": body.reason},
            conn=conn,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"tenant_id": body.tenant_id, "channel": body.channel, "active": body.active}


@app.get("/admin/kill-switch/{tenant_id}/{channel}")
def get_kill_switch(tenant_id: str, channel: str) -> dict:
    from .kill_switch import is_channel_killed

    conn = get_connection()
    try:
        killed, reason = is_channel_killed(conn, tenant_id, channel)
    finally:
        conn.close()
    return {"tenant_id": tenant_id, "channel": channel, "active": killed, "reason": reason}
