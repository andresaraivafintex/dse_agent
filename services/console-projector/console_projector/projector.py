"""Projector fase1 -> console_rm (Plano 06 §5).

Uma passada (`run_once`) processa cada fonte a partir do seu cursor e grava
saída + cursor NA MESMA transação (exactly-once efetivo; upserts idempotentes
— replay de lote é inofensivo; replay TOTAL com cursor 0 reconstrói o read
model do zero, que é também o plano de DR).

Fontes F0:
  - work_items        (cursor por updated_at — trigger set_updated_at)
  - audit_log         (cursor por id BIGSERIAL) -> timeline_events + last_event
  - model_call_ledger (cursor por id BIGSERIAL) -> runs_view
"""
from __future__ import annotations

import json
import logging
import os
import time

import psycopg2
import psycopg2.extras

from .mappers import map_audit_event, map_status, split_title

logger = logging.getLogger("console_projector")

_BATCH = int(os.environ.get("DSE_PROJECTOR_BATCH", "2000"))


def get_connection():
    dsn = os.environ.get(
        "DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
    )
    return psycopg2.connect(dsn)


# ---------------------------------------------------------------------------
# cursors
# ---------------------------------------------------------------------------

def _cursor(cur, source: str):
    cur.execute(
        "INSERT INTO console_rm.projection_cursor (source) VALUES (%s) "
        "ON CONFLICT (source) DO NOTHING",
        (source,),
    )
    cur.execute(
        "SELECT last_id, last_seen, last_key FROM console_rm.projection_cursor WHERE source = %s",
        (source,),
    )
    row = cur.fetchone()
    # cur é RealDictCursor (run_once) — acesso por nome, não posição.
    return int(row["last_id"]), row["last_seen"], row["last_key"]


def _advance(cur, source: str, *, last_id: int | None = None, last_seen=None,
             last_key: str | None = None) -> None:
    cur.execute(
        "UPDATE console_rm.projection_cursor SET last_id = COALESCE(%s, last_id), "
        "last_seen = COALESCE(%s, last_seen), last_key = COALESCE(%s, last_key), "
        "updated_at = now() WHERE source = %s",
        (last_id, last_seen, last_key, source),
    )


# ---------------------------------------------------------------------------
# work_items -> work_items_view
# ---------------------------------------------------------------------------

def _project_work_items(cur) -> int:
    # Keyset (updated_at, id): progresso ESTRITO — empates de timestamp são
    # desambiguados pela PK; drena até 0 (necessário para drain()/testes).
    _, last_seen, last_key = _cursor(cur, "work_items")
    if last_seen is None:
        cur.execute(
            "SELECT wi.*, COALESCE(wi.pr_url, t.pr_url) AS pr_url_eff, "
            "COALESCE(wi.pr_number, t.pr_number) AS pr_number_eff "
            "FROM work_items wi LEFT JOIN wse_pr_tracking t ON t.work_item_id = wi.id "
            "ORDER BY wi.updated_at, wi.id LIMIT %s", (_BATCH,))
    else:
        cur.execute(
            "SELECT wi.*, COALESCE(wi.pr_url, t.pr_url) AS pr_url_eff, "
            "COALESCE(wi.pr_number, t.pr_number) AS pr_number_eff "
            "FROM work_items wi LEFT JOIN wse_pr_tracking t ON t.work_item_id = wi.id "
            "WHERE (wi.updated_at, wi.id) > (%s, %s) "
            "ORDER BY wi.updated_at, wi.id LIMIT %s",
            (last_seen, last_key or "", _BATCH),
        )
    rows = cur.fetchall()
    if not rows:
        return 0
    for row in rows:
        _upsert_work_item(cur, row)
    tail = rows[-1]
    _advance(cur, "work_items", last_seen=tail["updated_at"], last_key=tail["id"])
    return len(rows)


def _upsert_work_item(cur, wi) -> None:
    try:
        status, phase = map_status(wi["status"])
    except KeyError:
        # status novo sem mapa: fail-visible (loga, não inventa nem derruba).
        logger.error("status fase1 sem mapa no console: %r (wi=%s)", wi["status"], wi["id"])
        return

    source_ref = wi["source_ref"] or {}
    number = source_ref.get("number") or source_ref.get("issue_number")
    source_id = f"{wi['repo']}#{number}" if wi["repo"] and number else (wi["repo"] or wi["id"][:18])

    # titulo/descricao: snapshot da admissao (TOCTOU) no outbox — sanitizado.
    cur.execute(
        "SELECT payload FROM ingest_events WHERE work_item_id = %s AND kind = 'task_request' "
        "ORDER BY id LIMIT 1",
        (wi["id"],),
    )
    ev = cur.fetchone()
    payload = (ev["payload"] if ev else None) or {}
    content = payload.get("sanitized_content") or payload.get("content_snapshot") or ""
    title, description = split_title(content, fallback=source_id)

    budget = wi["budget"] or {}
    budget_usd = budget.get("max_usd") if isinstance(budget, dict) else None

    cur.execute(
        """
        INSERT INTO console_rm.work_items_view (
            work_item_id, tenant_id, source, source_id, repo, base_branch, title,
            description, requester, data_class, status, current_phase,
            pr_number, pr_url, budget_usd, risk_class, task_class, created_at, updated_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (work_item_id) DO UPDATE SET
            status = EXCLUDED.status, current_phase = EXCLUDED.current_phase,
            title = EXCLUDED.title, description = EXCLUDED.description,
            pr_number = EXCLUDED.pr_number, pr_url = EXCLUDED.pr_url,
            risk_class = EXCLUDED.risk_class, budget_usd = EXCLUDED.budget_usd,
            task_class = EXCLUDED.task_class,
            updated_at = EXCLUDED.updated_at
        """,
        (
            wi["id"], wi["tenant_id"], wi["source"], source_id, wi["repo"],
            wi["base_branch"], title, description, wi["requester"], wi["data_class"],
            status, phase, wi.get("pr_number_eff") or wi["pr_number"],
            wi.get("pr_url_eff"), budget_usd,
            wi["risk_class"], wi.get("task_class"), wi["created_at"], wi["updated_at"],
        ),
    )


# ---------------------------------------------------------------------------
# work_item_evidence -> work_items_view (preview_status/url) — plano 08 §D (D5)
# ---------------------------------------------------------------------------

def _project_evidence(cur) -> int:
    """Reflete o estado do pipeline de evidência (preview/demo) na view. Keyset
    (updated_at, work_item_id) — mesmo padrão de _project_work_items; drena até
    0. Só ATUALIZA a linha da view (o work item sempre existe antes da
    evidência); se a linha ainda não foi projetada, o UPDATE é no-op e o cursor
    avança (a evidência entra quando o work item for projetado — idempotente)."""
    _, last_seen, last_key = _cursor(cur, "work_item_evidence")
    if last_seen is None:
        cur.execute(
            "SELECT work_item_id, preview_status, preview_url, demo_passed, "
            "video_artifact_key, trace_artifact_key, updated_at "
            "FROM work_item_evidence ORDER BY updated_at, work_item_id LIMIT %s", (_BATCH,))
    else:
        cur.execute(
            "SELECT work_item_id, preview_status, preview_url, demo_passed, "
            "video_artifact_key, trace_artifact_key, updated_at "
            "FROM work_item_evidence WHERE (updated_at, work_item_id) > (%s, %s) "
            "ORDER BY updated_at, work_item_id LIMIT %s",
            (last_seen, last_key or "", _BATCH),
        )
    rows = cur.fetchall()
    if not rows:
        return 0
    for row in rows:
        cur.execute(
            "UPDATE console_rm.work_items_view SET "
            "preview_status = %s, preview_url = %s, demo_passed = %s, "
            "video_artifact_key = %s, trace_artifact_key = %s "
            "WHERE work_item_id = %s",
            (row["preview_status"], row["preview_url"], row["demo_passed"],
             row["video_artifact_key"], row["trace_artifact_key"], row["work_item_id"]),
        )
    tail = rows[-1]
    _advance(cur, "work_item_evidence", last_seen=tail["updated_at"], last_key=tail["work_item_id"])
    return len(rows)


# ---------------------------------------------------------------------------
# audit_log -> timeline_events (+ last_event na view)
# ---------------------------------------------------------------------------

def _project_audit(cur) -> int:
    last_id, _, _ = _cursor(cur, "audit_log")
    cur.execute(
        "SELECT id, ts, work_item_id, tenant_id, actor, action, details FROM audit_log "
        "WHERE id > %s AND work_item_id IS NOT NULL ORDER BY id LIMIT %s",
        (last_id, _BATCH),
    )
    rows = cur.fetchall()
    if not rows:
        return 0
    last_event_by_wi: dict[str, tuple[str, object]] = {}
    for row in rows:
        ev_type, message = map_audit_event(row["action"], row["details"])
        _maybe_run_from_audit(cur, row)
        cur.execute(
            """
            INSERT INTO console_rm.timeline_events
                (audit_id, work_item_id, tenant_id, type, channel, message, data, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (work_item_id, audit_id) DO NOTHING
            """,
            (
                row["id"], row["work_item_id"], row["tenant_id"], ev_type, None,
                message, json.dumps(row["details"] or {}), row["ts"],
            ),
        )
        last_event_by_wi[row["work_item_id"]] = (message, row["ts"])
    for wi_id, (message, ts) in last_event_by_wi.items():
        cur.execute(
            "UPDATE console_rm.work_items_view SET last_event = %s, updated_at = GREATEST(updated_at, %s) "
            "WHERE work_item_id = %s",
            (message, ts, wi_id),
        )
    _advance(cur, "audit_log", last_id=rows[-1]["id"])
    return len(rows)


_AUDIT_COST_ACTIONS = {
    "planner_turn_completed": "planner",
    "coder_turn_completed": "coder",
    "tester_turn_completed": "tester",
    "l2_review_completed": "l2",
}


def _maybe_run_from_audit(cur, row) -> None:
    """Custo real dos turnos in-process (F0): o substrato fala com o gateway
    direto (nao passa pelo client Python que grava o ledger), entao o custo
    duravel esta em details->>'cost_usd' dos *_turn_completed. run_key
    'audit:<id>' nao colide com 'ledger:<id>'.

    Dedup (achado do painel mostrando ~2× o gasto real): o MESMO turno é
    auditado DUAS vezes — pela activity (system:sandbox-runtime) e pelo
    workflow (system:orchestrator). Só o row do ORQUESTRADOR vira run; o da
    activity fica na timeline mas não soma custo de novo."""
    engine = _AUDIT_COST_ACTIONS.get(row["action"])
    if not engine:
        return
    if row.get("actor") != "system:orchestrator":
        return
    details = row["details"] or {}
    cost = details.get("cost_usd")
    if cost in (None, ""):
        return
    cur.execute(
        """
        INSERT INTO console_rm.runs_view
            (run_key, work_item_id, tenant_id, engine, model, status,
             tokens_in, tokens_out, cost_usd, started_at, ended_at)
        VALUES (%s,%s,%s,%s,%s,'completed',%s,%s,%s,%s,%s)
        ON CONFLICT (run_key) DO NOTHING
        """,
        (
            f"audit:{row['id']}", row["work_item_id"], row["tenant_id"], engine,
            details.get("model"), int(details.get("tokens_in") or 0),
            int(details.get("tokens_out") or 0), float(cost), row["ts"], row["ts"],
        ),
    )


# ---------------------------------------------------------------------------
# model_call_ledger -> runs_view
# ---------------------------------------------------------------------------

def _project_ledger(cur) -> int:
    last_id, _, _ = _cursor(cur, "model_call_ledger")
    cur.execute(
        "SELECT id, tenant_id, work_item_id, stage, model, cost_usd, tokens_in, tokens_out, created_at "
        "FROM model_call_ledger WHERE id > %s ORDER BY id LIMIT %s",
        (last_id, _BATCH),
    )
    rows = cur.fetchall()
    if not rows:
        return 0
    for row in rows:
        cur.execute(
            """
            INSERT INTO console_rm.runs_view
                (run_key, work_item_id, tenant_id, engine, model, status,
                 tokens_in, tokens_out, cost_usd, started_at, ended_at)
            VALUES (%s,%s,%s,%s,%s,'completed',%s,%s,%s,%s,%s)
            ON CONFLICT (run_key) DO NOTHING
            """,
            (
                f"ledger:{row['id']}", row["work_item_id"], row["tenant_id"], row["stage"],
                row["model"], row["tokens_in"], row["tokens_out"], row["cost_usd"],
                row["created_at"], row["created_at"],
            ),
        )
    _advance(cur, "model_call_ledger", last_id=rows[-1]["id"])
    return len(rows)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def _refresh_cost_rollup(cur) -> int:
    """Plano 08 §E — recomputa o rollup de custo do SoR (model_call_ledger, a
    verdade de custo P8), agregando por dia×repo×model×task_class. Recompute
    total (DELETE+INSERT) → idempotente por construção (o teste de reconciliação
    garante rollup == ledger). Chamado só quando o ledger avançou nesta passada
    (barato o suficiente na escala do dev; um rollup incremental é o upgrade
    quando o volume exigir). Retorna o nº de células do rollup."""
    cur.execute("DELETE FROM console_rm.cost_rollup")
    cur.execute(
        """
        INSERT INTO console_rm.cost_rollup
            (tenant_id, day, repo, model, task_class, run_count, cost_usd, tokens_in, tokens_out)
        SELECT l.tenant_id,
               (l.created_at AT TIME ZONE 'UTC')::date AS day,
               COALESCE(NULLIF(wi.repo, ''), '(unknown)') AS repo,
               COALESCE(NULLIF(l.model, ''), '(unknown)') AS model,
               COALESCE(NULLIF(l.task_class, ''), 'chore') AS task_class,
               count(*), COALESCE(sum(l.cost_usd), 0),
               COALESCE(sum(l.tokens_in), 0), COALESCE(sum(l.tokens_out), 0)
        FROM model_call_ledger l
        LEFT JOIN work_items wi ON wi.id = l.work_item_id
        GROUP BY 1, 2, 3, 4, 5
        """
    )
    return cur.rowcount


def run_once(conn) -> dict[str, int]:
    """Uma passada por todas as fontes. Transação única: saída + cursores
    comitam juntos; qualquer erro faz rollback do lote inteiro (re-tentável)."""
    with conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            counts = {
                "work_items": _project_work_items(cur),
                # depois de work_items: a evidência ATUALIZA a linha da view (§D D5)
                "work_item_evidence": _project_evidence(cur),
                "audit_log": _project_audit(cur),
                "model_call_ledger": _project_ledger(cur),
            }
            # §E — rollup de custo: recomputa só quando o ledger avançou (evita
            # trabalho a cada tick ocioso). NÃO conta para a convergência do
            # drain (é derivado, não uma fonte com cursor).
            if counts["model_call_ledger"] > 0:
                _refresh_cost_rollup(cur)
    return counts


def drain(conn, *, max_iterations: int = 1000) -> None:
    """Roda run_once até todas as fontes zerarem (backfill/testes). Progresso
    é garantido pelos cursores estritos; o cap é so um backstop.

    §E: ao final força UM refresh do rollup de custo — o backfill/DR deve
    reconstruir o rollup mesmo quando o cursor do ledger já está no fim (nenhum
    row novo dispararia o refresh incremental do run_once). Recompute total é
    idempotente."""
    for _ in range(max_iterations):
        if not any(run_once(conn).values()):
            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    _refresh_cost_rollup(cur)
            return
    raise RuntimeError("drain não convergiu — cursor sem progresso?")


def main() -> None:  # pragma: no cover - loop de produção
    logging.basicConfig(level=logging.INFO)
    interval = float(os.environ.get("DSE_PROJECTOR_INTERVAL_SECONDS", "2"))
    conn = get_connection()
    logger.info("console-projector no ar (intervalo %.1fs)", interval)
    while True:
        try:
            counts = run_once(conn)
            if any(counts.values()):
                logger.info("projetado: %s", counts)
        except psycopg2.Error:
            logger.exception("erro de banco; reconectando")
            try:
                conn.close()
            except Exception:
                pass
            time.sleep(interval)
            conn = get_connection()
        time.sleep(interval)


if __name__ == "__main__":  # pragma: no cover
    main()
