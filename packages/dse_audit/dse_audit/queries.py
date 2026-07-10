"""Queries de leitura sobre o audit ledger (WSF-E1-T2 — extensão do WS-F sobre
o pacote da fundação `dse_audit`, conforme CONVENTIONS.md: "packages/dse_audit/
| Fundação (mínimo) -> WS-F estende [...] queries de reconstrução por WorkItem
(dse_audit.queries)").

Este módulo é puramente aditivo: não altera `dse_audit.client` (o único
caminho de escrita continua sendo `emit`). Duas funções cobrem o exit
criterion da Fase 1 ("first audit-based reconstruction exercise passes"):

  - `reconstruct_work_item_history(work_item_id)`: timeline completa de um
    WorkItem via um único SELECT ordenado por `ts`.
  - `export_audit_range(tenant_id, start, end)`: export compliance-grade
    (lista de dicts, serializável para CSV) de todas as linhas de audit de um
    tenant num período — para auditoria externa/regulador.

P8 (evidence over assertion): estas são as únicas duas formas suportadas de
"provar" o que aconteceu com um WorkItem — nunca reconstrua a partir de logs
de aplicação, sempre a partir do audit_log.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
from typing import Any

from .client import get_connection


def reconstruct_work_item_history(work_item_id: str, conn=None) -> list[dict[str, Any]]:
    """Reconstrói a timeline completa de um WorkItem a partir de UM único
    SELECT em `audit_log`, ordenado por `ts` (e por `id` como desempate
    estável quando dois eventos têm o mesmo timestamp).

    Retorna uma lista de dicts na ordem cronológica em que os eventos
    ocorreram: [{ts, actor, action, details, tenant_id}, ...]. Cada dict
    representa "quem fez o quê, quando" — o exercício de reconstrução por
    auditoria exigido pelo exit criterion da Fase 1.

    Não faz nenhuma suposição sobre quais ações existem (admitted, clarified,
    plan, implementing, l1_passed, pr_opened, review_approved, merged, ...) —
    é agnóstico ao vocabulário de ações, apenas devolve o que foi gravado.
    """
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ts, tenant_id, actor, action, details
                FROM audit_log
                WHERE work_item_id = %s
                ORDER BY ts ASC, id ASC
                """,
                (work_item_id,),
            )
            rows = cur.fetchall()
    finally:
        if owns_conn:
            conn.close()

    return [
        {
            "ts": row[0],
            "tenant_id": row[1],
            "actor": row[2],
            "action": row[3],
            "details": row[4],
        }
        for row in rows
    ]


def export_audit_range(
    tenant_id: str,
    start: dt.datetime,
    end: dt.datetime,
    conn=None,
) -> list[dict[str, Any]]:
    """Export compliance-grade de todas as linhas de audit de um tenant num
    período [start, end) — para produção sob demanda de auditoria externa/
    regulador (NFR-03). Um único SELECT, ordenado por `ts`.

    `start`/`end` devem ser timezone-aware (UTC recomendado); comparação é
    feita diretamente contra a coluna `ts` (TIMESTAMPTZ).
    """
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ts, tenant_id, work_item_id, actor, action, details
                FROM audit_log
                WHERE tenant_id = %s AND ts >= %s AND ts < %s
                ORDER BY ts ASC, id ASC
                """,
                (tenant_id, start, end),
            )
            rows = cur.fetchall()
    finally:
        if owns_conn:
            conn.close()

    return [
        {
            "ts": row[0],
            "tenant_id": row[1],
            "work_item_id": row[2],
            "actor": row[3],
            "action": row[4],
            "details": row[5],
        }
        for row in rows
    ]


def export_audit_range_csv(tenant_id: str, start: dt.datetime, end: dt.datetime, conn=None) -> str:
    """Mesmo export de `export_audit_range`, serializado como CSV (string)
    pronto para anexar a um relatório de auditoria. `details` (JSONB) é
    serializado como texto JSON compacto na coluna correspondente.
    """
    import json

    rows = export_audit_range(tenant_id, start, end, conn=conn)
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf, fieldnames=["ts", "tenant_id", "work_item_id", "actor", "action", "details"]
    )
    writer.writeheader()
    for row in rows:
        out = dict(row)
        out["ts"] = row["ts"].isoformat() if isinstance(row["ts"], dt.datetime) else row["ts"]
        out["details"] = json.dumps(row["details"], sort_keys=True)
        writer.writerow(out)
    return buf.getvalue()
