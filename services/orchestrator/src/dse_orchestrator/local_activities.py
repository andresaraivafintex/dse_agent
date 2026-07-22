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

import hashlib
import json
import logging
import os
from typing import Any

from temporalio import activity

from dse_contracts.activities import (
    ACTIVITY_EMIT_AUDIT,
    ACTIVITY_POST_TRACKING_COMMENT,
    PersistWorkItemStateInput,
)
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
# Fase 4 — insumo de skill-learning (episodio source=clarification, migracao
# 0019, dona WS-C; WS-B so INSERE o insumo) e metrica de qualidade de PR
# (pilot gate "PR quality thresholds").
LOCAL_ACTIVITY_RECORD_SKILL_EPISODE = "record_skill_episode"
LOCAL_ACTIVITY_EMIT_PR_QUALITY_METRIC = "emit_pr_quality_metric"

_DSN = os.environ.get(
    "DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
)


def _get_connection():
    import psycopg2

    return psycopg2.connect(_DSN)


@activity.defn(name=LOCAL_ACTIVITY_UPDATE_STATUS)
async def update_work_item_status(payload: dict[str, Any]) -> dict[str, Any]:
    """Projeta o estado do workflow em ``work_items`` de forma idempotente.

    O modelo aceita o payload historico minimo (work_item_id/status/pr_number)
    e campos novos opcionais. Plano/hash/expected_files sao derivados aqui,
    fora do sandbox deterministico do workflow. Reentregar a mesma Activity
    nao incrementa ``state_version`` quando o estado nao mudou.
    """
    inp = PersistWorkItemStateInput(**payload)
    work_item_id = inp.work_item_id
    plan_json = json.dumps(inp.plan) if inp.plan is not None else None
    expected_files = None
    plan_hash = None
    if inp.plan is not None:
        expected_files = json.dumps(list(inp.plan.get("expected_files") or []))
        canonical_plan = json.dumps(inp.plan, sort_keys=True, separators=(",", ":"))
        plan_hash = hashlib.sha256(canonical_plan.encode("utf-8")).hexdigest()
    attempts_json = (
        json.dumps(inp.validation_attempts) if inp.validation_attempts is not None else None
    )
    try:
        conn = _get_connection()
    except Exception as exc:  # pragma: no cover - so ocorre sem Postgres no ar
        logger.warning("update_work_item_status: sem conexao Postgres (%s); pulando persistencia", exc)
        return {"persisted": False}
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE work_items SET
                    status = COALESCE(%s, status),
                    pr_number = COALESCE(%s, pr_number),
                    pr_url = COALESCE(%s, pr_url),
                    plan = COALESCE(%s::jsonb, plan),
                    plan_hash = COALESCE(%s, plan_hash),
                    expected_files = COALESCE(%s::jsonb, expected_files),
                    risk_class = COALESCE(%s, risk_class),
                    base_sha = COALESCE(%s, base_sha),
                    head_sha = COALESCE(%s, head_sha),
                    ci_status = CASE
                        WHEN %s THEN NULL
                        ELSE COALESCE(%s, ci_status)
                    END,
                    last_error = COALESCE(%s, last_error),
                    validation_attempts = COALESCE(%s::jsonb, validation_attempts),
                    state_version = state_version + CASE
                        WHEN %s IS NOT NULL AND status IS DISTINCT FROM %s THEN 1
                        ELSE 0
                    END,
                    last_transition_at = CASE
                        WHEN %s IS NOT NULL AND status IS DISTINCT FROM %s THEN now()
                        ELSE last_transition_at
                    END
                WHERE id = %s
                RETURNING status, state_version, plan_hash, base_sha, head_sha, ci_status
                """,
                (
                    inp.status,
                    inp.pr_number,
                    inp.pr_url,
                    plan_json,
                    plan_hash,
                    expected_files,
                    inp.risk_class,
                    inp.base_sha,
                    inp.head_sha,
                    inp.clear_ci_status,
                    inp.ci_status,
                    inp.last_error,
                    attempts_json,
                    inp.status,
                    inp.status,
                    inp.status,
                    inp.status,
                    work_item_id,
                ),
            )
            row = cur.fetchone()
        conn.commit()
        if row is None:
            logger.info(
                "update_work_item_status: work_item_id=%s ainda nao existe em work_items (ok em teste isolado)",
                work_item_id,
            )
            return {"persisted": False}
        status, state_version, persisted_plan_hash, base_sha, head_sha, ci_status = row
        return {
            "persisted": True,
            "status": status,
            "state_version": state_version,
            "plan_hash": persisted_plan_hash,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "ci_status": ci_status,
        }
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
                "       risk_class, budget, source_ref, plan, plan_hash, expected_files, "
                "       base_sha, head_sha, pr_url, ci_status, state_version, last_error "
                "FROM work_items WHERE id = %s",
                (work_item_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"work_item_id={work_item_id!r} nao encontrado em work_items")
            # S1 (Fase 5): o conteudo da tarefa (titulo+corpo da issue) vive no
            # `ingest_events.payload` do evento de admissao (task_request), NAO
            # em work_items. Lemos aqui para o Planner/Coder receberem a
            # descricao real da tarefa — antes disto os agentes so recebiam
            # clarification_notes (vazio), sem saber o que construir.
            cur.execute(
                "SELECT payload FROM ingest_events "
                "WHERE work_item_id = %s AND kind = 'task_request' "
                "ORDER BY id ASC LIMIT 1",
                (work_item_id,),
            )
            ev = cur.fetchone()
        task_content = ""
        if ev and ev[0]:
            p = ev[0]  # JSONB -> dict (ConversationEvent serializado + sanitized_content)
            # a versao sanitizada e' a que segue ao modelo (WSA-E2-T3); cai
            # para o content_snapshot original se nao houver.
            task_content = (p.get("sanitized_content") or p.get("content_snapshot") or "").strip()
        (
            tenant_id, repo, base_branch, requester, data_class, pr_number,
            risk_class, budget, source_ref, plan, plan_hash, expected_files,
            base_sha, head_sha, pr_url, ci_status, state_version, last_error,
        ) = row
        # S3 (Fase 5): o numero da issue vive em source_ref (JSONB {repo, number})
        # — necessario para o outbound postar o comentario de status na issue certa.
        issue_number = None
        if isinstance(source_ref, dict):
            issue_number = source_ref.get("number") or source_ref.get("issue_number")
        return {
            "work_item_id": work_item_id,
            "tenant_id": tenant_id,
            "repo": repo,
            "base_branch": base_branch,
            "requester": requester,
            "data_class": data_class or "internal",
            "pr_number": pr_number,
            "risk_class": risk_class,
            "plan": plan or {},
            "plan_hash": plan_hash,
            "expected_files": expected_files or [],
            "base_sha": base_sha,
            "head_sha": head_sha,
            "pr_url": pr_url,
            "ci_status": ci_status,
            "state_version": int(state_version or 0),
            "last_error": last_error,
            "task_content": task_content,
            "issue_number": issue_number,
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


@activity.defn(name=LOCAL_ACTIVITY_RECORD_SKILL_EPISODE)
async def record_skill_episode(payload: dict[str, Any]) -> dict[str, Any]:
    """Fase 4 (WSC-E4-T2, source=clarification) — grava UM episodio de
    skill-learning em skill_episode (migracao 0019, tabela dona do WS-C; o WS-B
    so escreve o INSUMO). NENHUMA skill e criada/ativada aqui (fronteira testada
    em packages/contracts): o episodio e apenas o insumo governavel que a
    esteira de promocao do WS-C consome. `occurrence_n` e o contador tenant-wide
    de ocorrencias do mesmo `pattern_key` (proveniencia completa em JSONB).
    Idempotencia: cada recorrencia detectada gera uma linha nova (append-only,
    como o audit ledger) — a deduplicacao/gatilho de promocao e do WS-C."""
    tenant_id = payload["tenant_id"]
    pattern_key = payload["pattern_key"]
    try:
        conn = _get_connection()
    except Exception as exc:  # pragma: no cover - sem Postgres
        logger.warning("record_skill_episode: sem Postgres (%s); pulando insumo", exc)
        return {"persisted": False}
    try:
        import json

        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(MAX(occurrence_n), 0) FROM skill_episode "
                "WHERE tenant_id = %s AND pattern_key = %s",
                (tenant_id, pattern_key),
            )
            occurrence_n = int((cur.fetchone() or [0])[0] or 0) + 1
            cur.execute(
                """
                INSERT INTO skill_episode
                    (tenant_id, source, work_item_id, pattern_key, occurrence_n, provenance)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                RETURNING id
                """,
                (
                    tenant_id,
                    payload.get("source", "clarification"),
                    payload.get("work_item_id"),
                    pattern_key,
                    occurrence_n,
                    json.dumps(payload.get("provenance") or {}),
                ),
            )
            episode_id = cur.fetchone()[0]
        conn.commit()
        return {"persisted": True, "occurrence_n": occurrence_n, "episode_id": episode_id}
    finally:
        conn.close()


@activity.defn(name=LOCAL_ACTIVITY_EMIT_PR_QUALITY_METRIC)
async def emit_pr_quality_metric(payload: dict[str, Any]) -> None:
    """Fase 4 — emite as metricas OTel de qualidade de PR (pilot gate). A
    LEITURA (rounds/counts/tempo) e deterministica no workflow; a EMISSAO
    acontece aqui (I/O fora do sandbox — P1). Best-effort."""
    from dse_orchestrator import metrics

    metrics.record_pr_quality_metric(
        work_item_id=payload["work_item_id"],
        tenant_id=payload["tenant_id"],
        outcome=payload.get("outcome", "unknown"),
        review_rounds=int(payload.get("review_rounds", 0)),
        changes_requested_count=int(payload.get("changes_requested_count", 0)),
        evidence_refreshes=int(payload.get("evidence_refreshes", 0)),
        time_to_merge_seconds=payload.get("time_to_merge_seconds"),
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
    # S2 (Fase 5): "o que fazer" e satisfeito por um criterio de aceite
    # explicito OU por um corpo de tarefa substancial (issue bem descrita).
    # Heuristica determinística (P1): >= 40 chars de conteudo real conta como
    # descricao suficiente; abaixo disso (ex.: "arruma o bug") pede clarificacao.
    # Nunca um LLM decide isto.
    acceptance = (payload.get("acceptance_criteria") or "").strip()
    task_content = (payload.get("task_content") or "").strip()
    if not acceptance and len(task_content) < 40:
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


# Toda transição consequencial descreve o ESTADO ATUAL na superfície de origem
# (princípio de oversight barato — nunca deixar o humano no escuro em nenhuma
# plataforma). O fallback genérico garante que um status sem template ainda
# produza um comentário. Hoje GitHub; Slack/Jira reusam o mesmo vocabulário de
# status via seus próprios adapters de saída.
_STATUS_BODIES = {
    "needs_clarification": "🔎 O DSE precisa de esclarecimento antes de começar:\n\n{detail}",
    "awaiting_plan_approval": "📋 Plano pronto — aguardando aprovação humana (risco: {detail}).",
    "implementing": "⚙️ O DSE está implementando a mudança em um sandbox isolado.",
    "validating": "🧪 Implementação pronta — rodando validação (L1/L2) no sandbox.",
    "pr_ready": "✅ PR aberto com a mudança e evidência — pronto para revisão humana.",
    "pr_updated": "🔁 PR atualizado com o fix do review — pronto para nova revisão.",
    "done": "🎉 Merge feito por humano. Tarefa concluída.",
    "failed": "❌ A tarefa falhou e parou: {detail}",
    "escalated": (
        "⚠️ O DSE escalou esta tarefa para revisão humana e parou.\n\n"
        "**Motivo:** {detail}\n\n"
        "Revise a descrição / critérios de aceite e re-aplique a label `dse` "
        "para tentar novamente."
    ),
    "blocked": (
        "🚧 Bloqueado aguardando intervenção humana.\n\n**Motivo:** {detail}\n\n"
        "(ex.: nenhum aprovador resolvível — ajuste CODEOWNERS / access bundle.)"
    ),
}


@activity.defn(name=ACTIVITY_POST_TRACKING_COMMENT)
async def post_tracking_comment(payload: dict[str, Any]) -> dict[str, Any]:
    """Posta/edita O comentário de status único na superfície de ORIGEM
    (github/slack/jira), via o `/internal/status-comment` do adapter da fonte
    (todos usam a MESMA MutableCommentWriter). Auto-resolve o alvo de
    `work_items.source/source_ref` — os call sites só passam work_item_id +
    status (+ detail opcional). Determinístico (P1); best-effort (nunca derruba
    o workflow — o audit ledger é a fonte de verdade).

    C3 (relatório 07): generalizado além de github. Cada fonte tem seu adapter
    e seu campo de correlação:
      github -> {repo, issue_number}   @ DSE_ADAPTER_GITHUB_URL
      slack  -> {channel}              @ DSE_ADAPTER_SLACK_URL
      jira   -> {ticket_key}           @ DSE_ADAPTER_JIRA_URL
    Fonte desconhecida = no-op auditado."""
    work_item_id = payload["work_item_id"]
    tenant_id = payload.get("tenant_id", "")
    status = payload.get("status", "")
    detail = str(payload.get("detail") or "")
    body = payload.get("body")

    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT source, repo, source_ref FROM work_items WHERE id = %s", (work_item_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return {"ok": False, "reason": "work_item_not_found"}
    source, repo, source_ref = row
    source_ref = source_ref if isinstance(source_ref, dict) else {}

    target = _resolve_comment_target(source, repo, source_ref)
    if target is None:
        return {"ok": True, "skipped": f"source={source}_no_target"}

    if not body:
        template = _STATUS_BODIES.get(status, "Status do DSE: {status}")
        body = template.format(detail=detail or "—", status=status)

    adapter_url, extra_fields = target
    import httpx
    try:
        with httpx.Client(timeout=httpx.Timeout(8.0, connect=2.0)) as client:
            resp = client.post(
                f"{adapter_url}/internal/status-comment",
                json={"work_item_id": work_item_id, "body": body,
                      "actor": "system:orchestrator", **extra_fields},
            )
            resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — outbound é best-effort; nunca derruba o workflow
        logging.getLogger("dse_orchestrator").warning(
            "post_tracking_comment falhou para %s (%s): %s", work_item_id, source, exc
        )
        return {"ok": False, "reason": "adapter_unavailable", "error": str(exc)[:200]}
    dse_audit.emit(actor="system:orchestrator", action="tracking_comment_posted",
                   tenant_id=tenant_id, work_item_id=work_item_id,
                   details={"source": source, "status": status, **extra_fields})
    return {"ok": True}


def _resolve_comment_target(source, repo, source_ref: dict[str, Any]):
    """(adapter_url, campos_de_correlação) por fonte, ou None se não dá para
    endereçar (ex.: github sem issue_number). URLs lidas por-chamada (não no
    import) para os testes poderem sobrepor via env."""
    if source == "github":
        issue_number = source_ref.get("number") or source_ref.get("issue_number")
        if not repo or not issue_number:
            return None
        url = os.environ.get("DSE_ADAPTER_GITHUB_URL", "http://adapter-github:8802")
        return url, {"repo": repo, "issue_number": int(issue_number)}
    if source == "slack":
        channel = source_ref.get("channel")
        if not channel:
            return None
        url = os.environ.get("DSE_ADAPTER_SLACK_URL", "http://adapter-slack:8801")
        return url, {"channel": channel}
    if source == "jira":
        ticket_key = source_ref.get("ticket_key")
        if not ticket_key:
            return None
        url = os.environ.get("DSE_ADAPTER_JIRA_URL", "http://adapter-jira:8804")
        return url, {"ticket_key": ticket_key}
    return None


LOCAL_ACTIVITIES = [
    update_work_item_status,
    check_clarification_completeness,
    emit_audit_event,
    load_work_item,
    resolve_plan_approver,
    record_plan_approval,
    record_evidence_state,
    emit_history_metric,
    record_skill_episode,
    emit_pr_quality_metric,
    post_tracking_comment,
]
