"""Activities de propriedade do WS-B — nao fazem parte do contrato
cross-workstream de `dse_contracts.activities` (aquelas sao implementadas
por WS-C/WS-E). Estas tres existem para manter o workflow 100% deterministico
(disciplina P1: toda I/O — Postgres, audit — fica em Activity, nunca direto
no corpo do `@workflow.run`):

- `update_work_item_status`: unico caminho de escrita do WS-B na tabela
  compartilhada `work_items` (coluna `status`/`pr_number`). O workflow e o
  dono legitimo da maquina de estados (P1), entao e ele quem grava a
  transicao — outros servicos (adapters, admin UI) leem a mesma linha.
- `check_clarification_completeness`: checklist puro (repo? criterios de
  aceite? branch base?) sobre o snapshot do WorkItem — computado em Activity
  em vez de no corpo do workflow por disciplina (nao ha razao para nao ser
  Activity, e mante-lo assim deixa espaco para o checklist crescer sem
  arriscar nao-determinismo).
- `emit_audit_event` (nome estavel = `dse_contracts.activities.ACTIVITY_EMIT_AUDIT`):
  Temporal nao tem audit log proprio; esta e a Activity que TODOS os
  workstreams (inclusive o proprio orquestrador) chamam para gravar uma
  linha em `audit_log` via `dse_audit.emit` (P8).
"""
from __future__ import annotations

import logging
import os
from typing import Any

from temporalio import activity

from dse_contracts.activities import ACTIVITY_EMIT_AUDIT
import dse_audit

logger = logging.getLogger("dse_orchestrator.local_activities")

LOCAL_ACTIVITY_UPDATE_STATUS = "update_work_item_status"
LOCAL_ACTIVITY_CHECK_CLARIFICATION = "check_clarification_completeness"
LOCAL_ACTIVITY_LOAD_WORK_ITEM = "load_work_item"

_DSN = os.environ.get(
    "DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
)


def _get_connection():
    import psycopg2

    return psycopg2.connect(_DSN)


@activity.defn(name=LOCAL_ACTIVITY_UPDATE_STATUS)
async def update_work_item_status(payload: dict[str, Any]) -> dict[str, Any]:
    """UPDATE work_items SET status=..., pr_number=... WHERE id=....

    Idempotente: reescrever o mesmo status/pr_number duas vezes e um no-op
    logico. Se a linha nao existir ainda (ex.: teste isolado sem WS-A tendo
    rodado o intake), grava um aviso e segue — o workflow e a fonte de
    verdade da maquina de estados mesmo quando a projecao em Postgres nao
    esta disponivel (ex.: ambiente de teste unitario do WS-B sozinho).
    """
    work_item_id = payload["work_item_id"]
    status = payload["status"]
    pr_number = payload.get("pr_number")
    try:
        conn = _get_connection()
    except Exception as exc:  # pragma: no cover - so ocorre sem Postgres no ar
        logger.warning("update_work_item_status: sem conexao Postgres (%s); pulando persistencia", exc)
        return {"persisted": False}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE work_items SET status = %s, pr_number = COALESCE(%s, pr_number) WHERE id = %s",
                (status, pr_number, work_item_id),
            )
            updated = cur.rowcount
        conn.commit()
        if updated == 0:
            logger.info(
                "update_work_item_status: work_item_id=%s ainda nao existe em work_items (ok em teste isolado)",
                work_item_id,
            )
        return {"persisted": updated > 0}
    finally:
        conn.close()


@activity.defn(name=LOCAL_ACTIVITY_LOAD_WORK_ITEM)
async def load_work_item(payload: dict[str, Any]) -> dict[str, Any]:
    """Le a linha de `work_items` por id — usada SOMENTE quando o workflow e
    iniciado so com o `work_item_id` (string), em vez do
    `WorkItemLifecycleInput` completo (ver `workflows.py::_coerce_input` e o
    README, secao "Contrato de start_workflow assumido"). WS-A e quem grava
    a linha original em `work_items` antes de chamar `StartWorkflow`."""
    work_item_id = payload["work_item_id"]
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tenant_id, repo, base_branch, requester, data_class, pr_number "
                "FROM work_items WHERE id = %s",
                (work_item_id,),
            )
            row = cur.fetchone()
        if row is None:
            raise ValueError(f"work_item_id={work_item_id!r} nao encontrado em work_items")
        tenant_id, repo, base_branch, requester, data_class, pr_number = row
        return {
            "work_item_id": work_item_id,
            "tenant_id": tenant_id,
            "repo": repo,
            "base_branch": base_branch,
            "requester": requester,
            "data_class": data_class or "internal",
            "pr_number": pr_number,
        }
    finally:
        conn.close()


@activity.defn(name=LOCAL_ACTIVITY_CHECK_CLARIFICATION)
async def check_clarification_completeness(payload: dict[str, Any]) -> dict[str, Any]:
    """Checklist deterministico e simples por task-class (Fase 1: uma unica
    task-class "default"). Nunca um LLM decide isto (P1)."""
    missing: list[str] = []
    if not payload.get("repo"):
        missing.append("repo")
    if not payload.get("base_branch"):
        missing.append("base_branch")
    if not (payload.get("acceptance_criteria") or "").strip():
        missing.append("acceptance_criteria")
    return {"complete": not missing, "missing": missing}


@activity.defn(name=ACTIVITY_EMIT_AUDIT)
async def emit_audit_event(payload: dict[str, Any]) -> None:
    """Unica ponte entre o mundo determinístico do workflow e o audit ledger
    (P8). Usa `dse_audit.emit` por baixo — nunca escreve em audit_log direto.
    """
    dse_audit.emit(
        actor=payload["actor"],
        action=payload["action"],
        tenant_id=payload["tenant_id"],
        work_item_id=payload.get("work_item_id"),
        details=payload.get("details") or {},
    )


LOCAL_ACTIVITIES = [
    update_work_item_status,
    check_clarification_completeness,
    emit_audit_event,
    load_work_item,
]
