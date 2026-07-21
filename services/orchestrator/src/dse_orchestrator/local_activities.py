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

from dse_orchestrator import policy

logger = logging.getLogger("dse_orchestrator.local_activities")

LOCAL_ACTIVITY_UPDATE_STATUS = "update_work_item_status"
LOCAL_ACTIVITY_CHECK_CLARIFICATION = "check_clarification_completeness"
LOCAL_ACTIVITY_LOAD_WORK_ITEM = "load_work_item"
# Fase 2 (WSB-E3-T2) — resolucao de aprovador (I/O: DB + CODEOWNERS) e
# projecao duravel do gate (WSB migracao 0009).
LOCAL_ACTIVITY_RESOLVE_APPROVER = "resolve_plan_approver"
LOCAL_ACTIVITY_RECORD_GATE = "record_plan_approval"
# Fase 3 — projecao duravel do estado do pipeline de evidencia (migracao 0014)
# e emissao da metrica OTel de tamanho de history (ALERTING-RULES.md §3).
LOCAL_ACTIVITY_RECORD_EVIDENCE = "record_evidence_state"
LOCAL_ACTIVITY_EMIT_HISTORY_METRIC = "emit_history_metric"

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
                "SELECT tenant_id, repo, base_branch, requester, data_class, pr_number, "
                "       risk_class, budget "
                "FROM work_items WHERE id = %s",
                (work_item_id,),
            )
            row = cur.fetchone()
        if row is None:
            raise ValueError(f"work_item_id={work_item_id!r} nao encontrado em work_items")
        tenant_id, repo, base_branch, requester, data_class, pr_number, risk_class, budget = row
        return {
            "work_item_id": work_item_id,
            "tenant_id": tenant_id,
            "repo": repo,
            "base_branch": base_branch,
            "requester": requester,
            "data_class": data_class or "internal",
            "pr_number": pr_number,
            "risk_class": risk_class,
            # WSB-E4-T1: budget lido na admissao. `budget` e o JSONB de
            # work_items (default '{}'). A chave "max_usd" e o teto agregado.
            "budget": budget or {},
        }
    finally:
        conn.close()


@activity.defn(name=LOCAL_ACTIVITY_RESOLVE_APPROVER)
async def resolve_plan_approver(payload: dict[str, Any]) -> dict[str, Any]:
    """WSB-E3-T2 — cascata de resolucao de aprovador: CODEOWNERS -> designated
    approvers do access bundle (WS-F, `dse_access_bundle`). Retorna a PRIMEIRA
    fonte nao-vazia. Cascata VAZIA retorna `[]` — o workflow trata isso como
    Blocked + escalacao, JAMAIS auto-aprova por ausencia (P1/P3).

    I/O aqui (DB + CODEOWNERS) e por isso ser uma Activity e nao codigo de
    workflow. Aprovadores offboardados (dse_console_identity.active=false) sao
    filtrados quando a tabela de identidade do console existir (WS-F)."""
    tenant_id = payload["tenant_id"]
    repo = payload.get("repo")
    channel = payload.get("channel")

    # --- fonte 1: CODEOWNERS (reader trocavel; producao = adapter GitHub) ---
    codeowners: list[str] = []
    reader = policy._codeowners_reader
    if reader is not None:
        try:
            text = reader(tenant_id, repo)
            codeowners = policy.parse_codeowners_owners(text)
        except Exception as exc:  # pragma: no cover - defensivo
            logger.warning("codeowners_reader falhou (%s); seguindo p/ access bundle", exc)
    if codeowners:
        return {"approvers": codeowners, "source": "codeowners"}

    # --- fonte 2: access bundle (WS-F) — designated_approvers ---
    try:
        conn = _get_connection()
    except Exception as exc:  # pragma: no cover - sem Postgres
        logger.warning("resolve_plan_approver: sem Postgres (%s)", exc)
        return {"approvers": [], "source": "none"}
    try:
        approvers: list[str] = []
        try:
            with conn.cursor() as cur:
                # resolucao: canal-especifico primeiro, senao default do tenant (channel NULL)
                cur.execute(
                    """
                    SELECT designated_approvers
                    FROM dse_access_bundle
                    WHERE tenant_id = %s AND enabled = true
                      AND (channel = %s OR channel IS NULL)
                    ORDER BY (channel IS NOT NULL) DESC
                    LIMIT 1
                    """,
                    (tenant_id, channel),
                )
                row = cur.fetchone()
            if row and row[0]:
                approvers = [str(a) for a in row[0]]
        except Exception as exc:
            # WS-F ainda pode nao ter criado a tabela (build paralelo) — trata
            # como fonte vazia, nunca como erro fatal do gate.
            conn.rollback()
            logger.warning("dse_access_bundle indisponivel (%s); fonte tratada como vazia", exc)
            approvers = []

        # filtra offboardados via dse_console_identity.active, se a tabela existir
        if approvers:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT principal_id FROM dse_console_identity "
                        "WHERE principal_id = ANY(%s) AND active = false",
                        (approvers,),
                    )
                    inactive = {r[0] for r in cur.fetchall()}
                if inactive:
                    approvers = [a for a in approvers if a not in inactive]
            except Exception:
                conn.rollback()  # tabela ausente -> sem filtro (nao bloqueia)
        return {"approvers": approvers, "source": "access_bundle" if approvers else "none"}
    finally:
        conn.close()


@activity.defn(name=LOCAL_ACTIVITY_RECORD_GATE)
async def record_plan_approval(payload: dict[str, Any]) -> dict[str, Any]:
    """WSB-E3-T2/T3 — projecao duravel do gate (migracao 0009). Upsert
    idempotente por work_item_id. NAO substitui o audit ledger (o workflow
    tambem chama emit_audit_event) — e a projecao mutavel consultavel pelo
    queue board/operadores."""
    work_item_id = payload["work_item_id"]
    try:
        conn = _get_connection()
    except Exception as exc:  # pragma: no cover
        logger.warning("record_plan_approval: sem Postgres (%s); pulando projecao", exc)
        return {"persisted": False}
    try:
        import json

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO plan_approval_gate
                    (work_item_id, tenant_id, risk_class, status, auto_approved,
                     resolved_approvers, decided_by, rejection_route, justification, plan_round)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                ON CONFLICT (work_item_id) DO UPDATE SET
                    risk_class = EXCLUDED.risk_class,
                    status = EXCLUDED.status,
                    auto_approved = EXCLUDED.auto_approved,
                    resolved_approvers = EXCLUDED.resolved_approvers,
                    decided_by = EXCLUDED.decided_by,
                    rejection_route = EXCLUDED.rejection_route,
                    justification = EXCLUDED.justification,
                    plan_round = EXCLUDED.plan_round
                """,
                (
                    work_item_id,
                    payload["tenant_id"],
                    payload["risk_class"],
                    payload["status"],
                    bool(payload.get("auto_approved", False)),
                    json.dumps(payload.get("resolved_approvers", [])),
                    payload.get("decided_by"),
                    payload.get("rejection_route"),
                    payload.get("justification"),
                    int(payload.get("plan_round", 0)),
                ),
            )
        conn.commit()
        return {"persisted": True}
    finally:
        conn.close()


@activity.defn(name=LOCAL_ACTIVITY_RECORD_EVIDENCE)
async def record_evidence_state(payload: dict[str, Any]) -> dict[str, Any]:
    """Fase 3 — projecao duravel do estado do pipeline de evidencia (migracao
    0014, tabela work_item_evidence). Upsert idempotente por work_item_id.
    NAO substitui o audit ledger (P8): o workflow emite os eventos de evidencia
    via emit_audit_event; esta tabela e a projecao mutavel consultavel pelo
    queue board (WS-F)/operadores ("qual o preview/video mais recente?")."""
    work_item_id = payload["work_item_id"]
    try:
        conn = _get_connection()
    except Exception as exc:  # pragma: no cover
        logger.warning("record_evidence_state: sem Postgres (%s); pulando projecao", exc)
        return {"persisted": False}
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO work_item_evidence
                    (work_item_id, tenant_id, preview_status, preview_url, demo_passed,
                     video_artifact_key, trace_artifact_key, visual_baseline_key,
                     refresh_count, last_refresh_reason, detail)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (work_item_id) DO UPDATE SET
                    preview_status = EXCLUDED.preview_status,
                    preview_url = EXCLUDED.preview_url,
                    demo_passed = EXCLUDED.demo_passed,
                    video_artifact_key = EXCLUDED.video_artifact_key,
                    trace_artifact_key = EXCLUDED.trace_artifact_key,
                    visual_baseline_key = EXCLUDED.visual_baseline_key,
                    refresh_count = EXCLUDED.refresh_count,
                    last_refresh_reason = EXCLUDED.last_refresh_reason,
                    detail = EXCLUDED.detail
                """,
                (
                    work_item_id,
                    payload["tenant_id"],
                    payload.get("preview_status"),
                    payload.get("preview_url"),
                    payload.get("demo_passed"),
                    payload.get("video_artifact_key"),
                    payload.get("trace_artifact_key"),
                    payload.get("visual_baseline_key"),
                    int(payload.get("refresh_count", 0)),
                    payload.get("last_refresh_reason"),
                    payload.get("detail"),
                ),
            )
        conn.commit()
        return {"persisted": True}
    finally:
        conn.close()


@activity.defn(name=LOCAL_ACTIVITY_EMIT_HISTORY_METRIC)
async def emit_history_metric(payload: dict[str, Any]) -> None:
    """Fase 3 — ativacao do alerta de history (ALERTING-RULES.md §3, com WS-F).
    O workflow LE o tamanho do history de forma deterministica
    (workflow.info().get_current_history_length()/size()) e esta Activity
    EMITE a metrica OTel para o collector (I/O fora do sandbox — P1).
    Best-effort: o workflow trata falha aqui como nao-fatal."""
    from dse_orchestrator import metrics

    metrics.record_history_metric(
        work_item_id=payload["work_item_id"],
        tenant_id=payload["tenant_id"],
        phase=payload.get("phase", "unknown"),
        checkpoint=payload.get("checkpoint", "unknown"),
        history_length=int(payload.get("history_length", 0)),
        history_size_bytes=int(payload.get("history_size_bytes", 0)),
        continue_as_new_count=int(payload.get("continue_as_new_count", 0)),
    )


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
    resolve_plan_approver,
    record_plan_approval,
    record_evidence_state,
    emit_history_metric,
]
