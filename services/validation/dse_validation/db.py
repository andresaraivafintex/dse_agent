"""Acesso Postgres para as tabelas de `migrations/0006_wse.sql`. Usa a role
`dse_app` (mesma convenção de `dse_audit`/`dse_identity`) — sem framework de
ORM (P7 boring-first)."""
from __future__ import annotations

import json
import os
from typing import Any

import psycopg2

_DSN = os.environ.get(
    "DSE_VALIDATION_DATABASE_URL",
    os.environ.get("DSE_DATABASE_URL", "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"),
)


def get_connection():
    return psycopg2.connect(_DSN)


# ---------------------------------------------------------------------------
# validation_runs (WSE-E1) — 1 linha de evidência por execução do L1.
# ---------------------------------------------------------------------------
def record_validation_run(work_item_id: str, tenant_id: str, passed: bool, findings: list[dict[str, Any]]) -> int:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO validation_runs (work_item_id, tenant_id, passed, findings)
                VALUES (%s, %s, %s, %s::jsonb)
                RETURNING id
                """,
                (work_item_id, tenant_id, passed, json.dumps(findings)),
            )
            run_id = cur.fetchone()[0]
        conn.commit()
        return run_id
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# wse_pr_tracking (WSE-E3-T6) — garante idempotência de "1 PR por WorkItem".
# ---------------------------------------------------------------------------
def get_tracked_pr(work_item_id: str) -> dict[str, Any] | None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT work_item_id, tenant_id, repo, branch, pr_number, pr_url, compare_url "
                "FROM wse_pr_tracking WHERE work_item_id = %s",
                (work_item_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        keys = ["work_item_id", "tenant_id", "repo", "branch", "pr_number", "pr_url", "compare_url"]
        return dict(zip(keys, row))
    finally:
        conn.close()


def save_tracked_pr(
    work_item_id: str,
    tenant_id: str,
    repo: str,
    branch: str,
    pr_number: int | None,
    pr_url: str,
    compare_url: str | None = None,
) -> None:
    # Fase 2 (WSE-E3-T8): pr_number pode ser NULL no modo estrito (só compare
    # link postado, PR ainda não aberto por humano). `pr_url` guarda a melhor
    # URL conhecida (compare link enquanto pr_number IS NULL, depois a do PR).
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO wse_pr_tracking
                    (work_item_id, tenant_id, repo, branch, pr_number, pr_url, compare_url)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (work_item_id) DO UPDATE SET
                    pr_number = EXCLUDED.pr_number, pr_url = EXCLUDED.pr_url,
                    compare_url = EXCLUDED.compare_url
                """,
                (work_item_id, tenant_id, repo, branch, pr_number, pr_url, compare_url),
            )
        conn.commit()
    finally:
        conn.close()


def adopt_tracked_pr(work_item_id: str, pr_number: int, pr_url: str) -> None:
    """WSE-E3-T8 — modo estrito: um humano abriu o PR a partir do compare link;
    preenche pr_number/pr_url na linha existente SEM criar outra. Idempotente:
    só grava se ainda não havia um pr_number (o primeiro humano que abre vence;
    reexecuções não sobrescrevem)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE wse_pr_tracking
                   SET pr_number = %s, pr_url = %s
                 WHERE work_item_id = %s AND pr_number IS NULL
                """,
                (pr_number, pr_url, work_item_id),
            )
        conn.commit()
    finally:
        conn.close()


def _delete_tracked_pr_for_test(work_item_id: str) -> None:
    """Só para testes — simula "crash antes de persistir" apagando a linha
    sem apagar o estado do lado GitHub (fake). Não é chamado em produção."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM wse_pr_tracking WHERE work_item_id = %s", (work_item_id,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# wse_comment_refs — CommentStateStore (dse_contracts.mutable_comment).
# ---------------------------------------------------------------------------
class PostgresCommentStateStore:
    def get_ref(self, work_item_id: str, surface: str) -> str | None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT comment_ref FROM wse_comment_refs WHERE work_item_id = %s AND surface = %s",
                    (work_item_id, surface),
                )
                row = cur.fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def save_ref(self, work_item_id: str, surface: str, comment_ref: str) -> None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO wse_comment_refs (work_item_id, surface, comment_ref)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (work_item_id, surface) DO UPDATE SET
                        comment_ref = EXCLUDED.comment_ref, updated_at = now()
                    """,
                    (work_item_id, surface, comment_ref),
                )
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# wse_ci_status (WSE-E4-T9a).
# ---------------------------------------------------------------------------
def save_ci_status(work_item_id: str, pr_number: int, status: str, detail: dict[str, Any]) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO wse_ci_status (work_item_id, pr_number, status, detail)
                VALUES (%s, %s, %s, %s::jsonb)
                ON CONFLICT (work_item_id) DO UPDATE SET
                    pr_number = EXCLUDED.pr_number, status = EXCLUDED.status,
                    detail = EXCLUDED.detail, updated_at = now()
                """,
                (work_item_id, pr_number, status, json.dumps(detail)),
            )
        conn.commit()
    finally:
        conn.close()


def get_ci_status(work_item_id: str) -> dict[str, Any] | None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT work_item_id, pr_number, status, detail FROM wse_ci_status WHERE work_item_id = %s",
                (work_item_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return {"work_item_id": row[0], "pr_number": row[1], "status": row[2], "detail": row[3]}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# wse_l2_reviews (WSE-E2-T4) — evidência de cada execução da sessão Reviewer L2.
# ---------------------------------------------------------------------------
def record_l2_review(
    work_item_id: str,
    tenant_id: str,
    iteration: int,
    passed: bool,
    objections: list[str],
    cost_usd: float,
) -> int:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO wse_l2_reviews
                    (work_item_id, tenant_id, iteration, passed, objections, cost_usd)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s)
                RETURNING id
                """,
                (work_item_id, tenant_id, iteration, passed, json.dumps(objections), cost_usd),
            )
            review_id = cur.fetchone()[0]
        conn.commit()
        return review_id
    finally:
        conn.close()


def get_l2_reviews(work_item_id: str) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT iteration, passed, objections, cost_usd, run_at "
                "FROM wse_l2_reviews WHERE work_item_id = %s ORDER BY run_at ASC, id ASC",
                (work_item_id,),
            )
            rows = cur.fetchall()
        return [
            {"iteration": r[0], "passed": r[1], "objections": r[2], "cost_usd": float(r[3]), "run_at": r[4]}
            for r in rows
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# wse_fix_loops (WSE-E2-T5) — contador durável do loop bounded L2->Coder.
# ---------------------------------------------------------------------------
def get_fix_loop(work_item_id: str) -> dict[str, Any] | None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT work_item_id, tenant_id, iterations, spent_usd, exhausted "
                "FROM wse_fix_loops WHERE work_item_id = %s",
                (work_item_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return {
            "work_item_id": row[0],
            "tenant_id": row[1],
            "iterations": row[2],
            "spent_usd": float(row[3]),
            "exhausted": row[4],
        }
    finally:
        conn.close()


def upsert_fix_loop(
    work_item_id: str,
    tenant_id: str,
    iterations: int,
    spent_usd: float,
    exhausted: bool,
) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO wse_fix_loops (work_item_id, tenant_id, iterations, spent_usd, exhausted)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (work_item_id) DO UPDATE SET
                    iterations = EXCLUDED.iterations, spent_usd = EXCLUDED.spent_usd,
                    exhausted = EXCLUDED.exhausted, updated_at = now()
                """,
                (work_item_id, tenant_id, iterations, spent_usd, exhausted),
            )
        conn.commit()
    finally:
        conn.close()


# ===========================================================================
# Fase 3 (0017_wse3.sql)
# ===========================================================================

# ---------------------------------------------------------------------------
# wse_artifacts + wse_artifact_access_log (WSE-E5-T12)
# ---------------------------------------------------------------------------
def record_artifact(
    *,
    work_item_id: str,
    tenant_id: str,
    kind: str,
    bucket: str,
    store_key: str,
    content_type: str,
    size_bytes: int,
    multipart: bool,
    ttl_seconds: int,
    expires_at,
) -> int:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO wse_artifacts
                    (work_item_id, tenant_id, kind, bucket, store_key, content_type,
                     size_bytes, multipart, ttl_seconds, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (bucket, store_key) DO UPDATE SET
                    content_type = EXCLUDED.content_type, size_bytes = EXCLUDED.size_bytes,
                    multipart = EXCLUDED.multipart, ttl_seconds = EXCLUDED.ttl_seconds,
                    expires_at = EXCLUDED.expires_at,
                    quarantined_at = NULL, quarantine_key = NULL
                RETURNING id
                """,
                (work_item_id, tenant_id, kind, bucket, store_key, content_type,
                 size_bytes, multipart, ttl_seconds, expires_at),
            )
            artifact_id = cur.fetchone()[0]
        conn.commit()
        return artifact_id
    finally:
        conn.close()


_ARTIFACT_COLS = (
    "id, work_item_id, tenant_id, kind, bucket, store_key, content_type, "
    "size_bytes, multipart, ttl_seconds, expires_at, quarantined_at, quarantine_key"
)


def _artifact_row_to_dict(row) -> dict[str, Any]:
    keys = ["id", "work_item_id", "tenant_id", "kind", "bucket", "store_key", "content_type",
            "size_bytes", "multipart", "ttl_seconds", "expires_at", "quarantined_at", "quarantine_key"]
    return dict(zip(keys, row))


def get_artifact(work_item_id: str, store_key: str) -> dict[str, Any] | None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_ARTIFACT_COLS} FROM wse_artifacts WHERE work_item_id = %s AND store_key = %s",
                (work_item_id, store_key),
            )
            row = cur.fetchone()
        return _artifact_row_to_dict(row) if row else None
    finally:
        conn.close()


def list_artifacts(work_item_id: str) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_ARTIFACT_COLS} FROM wse_artifacts WHERE work_item_id = %s ORDER BY id",
                (work_item_id,),
            )
            rows = cur.fetchall()
        return [_artifact_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def mark_artifact_quarantined(artifact_id: int, *, quarantine_key: str) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE wse_artifacts
                   SET quarantined_at = now(), quarantine_key = %s
                 WHERE id = %s AND quarantined_at IS NULL
                """,
                (quarantine_key, artifact_id),
            )
        conn.commit()
    finally:
        conn.close()


def record_artifact_access(
    *,
    artifact_id: int,
    work_item_id: str,
    tenant_id: str,
    pr_number: int | None,
    accessor: str,
    via: str,
) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO wse_artifact_access_log
                    (artifact_id, work_item_id, tenant_id, pr_number, accessor, via)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (artifact_id, work_item_id, tenant_id, pr_number, accessor, via),
            )
        conn.commit()
    finally:
        conn.close()


def list_artifact_accesses(work_item_id: str) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT artifact_id, pr_number, accessor, via, accessed_at "
                "FROM wse_artifact_access_log WHERE work_item_id = %s ORDER BY id",
                (work_item_id,),
            )
            rows = cur.fetchall()
        return [
            {"artifact_id": r[0], "pr_number": r[1], "accessor": r[2], "via": r[3], "accessed_at": r[4]}
            for r in rows
        ]
    finally:
        conn.close()


def list_quarantined_work_items_with_artifacts() -> list[str]:
    """Work items ATIVOS em quarentena (tabela do WS-F, data-plane — não editamos
    a migração dele) que ainda têm artefato não-quarantinado no store."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT a.work_item_id
                  FROM wse_artifacts a
                  JOIN dse_work_item_quarantine q
                    ON q.work_item_id = a.work_item_id AND q.released_at IS NULL
                 WHERE a.quarantined_at IS NULL
                """
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# wse_previews + wse_preview_caps (WSE-E4-T10 / ADR-26)
# ---------------------------------------------------------------------------
def upsert_preview(
    *,
    work_item_id: str,
    tenant_id: str,
    pr_number: int,
    repo: str,
    status: str,
    namespace: str | None = None,
    url: str | None = None,
    detail: str = "",
    ttl_seconds: int = 3600,
    expires_at=None,
) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO wse_previews
                    (work_item_id, tenant_id, pr_number, repo, status, namespace, url,
                     detail, ttl_seconds, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (work_item_id) DO UPDATE SET
                    pr_number = EXCLUDED.pr_number, status = EXCLUDED.status,
                    namespace = EXCLUDED.namespace, url = EXCLUDED.url,
                    detail = EXCLUDED.detail, ttl_seconds = EXCLUDED.ttl_seconds,
                    expires_at = EXCLUDED.expires_at,
                    reaped_at = CASE WHEN EXCLUDED.status = 'created' THEN NULL
                                     ELSE wse_previews.reaped_at END,
                    updated_at = now()
                """,
                (work_item_id, tenant_id, pr_number, repo, status, namespace, url,
                 detail, ttl_seconds, expires_at),
            )
        conn.commit()
    finally:
        conn.close()


def get_preview(work_item_id: str) -> dict[str, Any] | None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT work_item_id, tenant_id, pr_number, repo, status, namespace, url, "
                "detail, ttl_seconds, expires_at, reaped_at FROM wse_previews WHERE work_item_id = %s",
                (work_item_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        keys = ["work_item_id", "tenant_id", "pr_number", "repo", "status", "namespace",
                "url", "detail", "ttl_seconds", "expires_at", "reaped_at"]
        return dict(zip(keys, row))
    finally:
        conn.close()


def count_active_previews(tenant_id: str) -> int:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM wse_previews "
                "WHERE tenant_id = %s AND status = 'created' AND reaped_at IS NULL",
                (tenant_id,),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()


def get_preview_cap(tenant_id: str) -> int | None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT max_concurrent FROM wse_preview_caps WHERE tenant_id = %s", (tenant_id,))
            row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def set_preview_cap(tenant_id: str, max_concurrent: int) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO wse_preview_caps (tenant_id, max_concurrent) VALUES (%s, %s)
                ON CONFLICT (tenant_id) DO UPDATE SET max_concurrent = EXCLUDED.max_concurrent
                """,
                (tenant_id, max_concurrent),
            )
        conn.commit()
    finally:
        conn.close()


def list_expired_previews(now=None) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT work_item_id, tenant_id, namespace FROM wse_previews
                 WHERE status = 'created' AND reaped_at IS NULL
                   AND expires_at IS NOT NULL AND expires_at <= COALESCE(%s, now())
                """,
                (now,),
            )
            rows = cur.fetchall()
        return [{"work_item_id": r[0], "tenant_id": r[1], "namespace": r[2]} for r in rows]
    finally:
        conn.close()


def mark_preview_reaped(work_item_id: str) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE wse_previews SET status = 'reaped', reaped_at = now(), updated_at = now() "
                "WHERE work_item_id = %s AND reaped_at IS NULL",
                (work_item_id,),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# wse_ci_reruns + wse_ci_repair_episodes (WSE-E4-T9b)
# ---------------------------------------------------------------------------
def record_ci_rerun(
    *,
    work_item_id: str,
    tenant_id: str,
    pr_number: int,
    fix_commit_sha: str,
    check_run_ids: list,
    check_names: list,
) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO wse_ci_reruns
                    (work_item_id, tenant_id, pr_number, fix_commit_sha, check_run_ids, check_names)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb)
                """,
                (work_item_id, tenant_id, pr_number, fix_commit_sha,
                 json.dumps(check_run_ids), json.dumps(check_names)),
            )
        conn.commit()
    finally:
        conn.close()


def list_ci_reruns(work_item_id: str) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pr_number, fix_commit_sha, check_run_ids, check_names, requested_at "
                "FROM wse_ci_reruns WHERE work_item_id = %s ORDER BY id",
                (work_item_id,),
            )
            rows = cur.fetchall()
        return [
            {"pr_number": r[0], "fix_commit_sha": r[1], "check_run_ids": r[2],
             "check_names": r[3], "requested_at": r[4]}
            for r in rows
        ]
    finally:
        conn.close()


def record_ci_repair_episode(
    *,
    tenant_id: str,
    work_item_id: str,
    check_name: str,
    failure_signature: str,
    fix_commit_sha: str,
    provenance: dict,
) -> dict[str, Any]:
    """Grava o episódio de CI-repair (tenant-scoped) com `occurrence_n` =
    nº de ocorrências do MESMO padrão (tenant, assinatura) até aqui, inclusive.
    NENHUMA skill é criada/ativada — só o episódio (promoção é Fase 4)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM wse_ci_repair_episodes "
                "WHERE tenant_id = %s AND failure_signature = %s",
                (tenant_id, failure_signature),
            )
            occurrence_n = cur.fetchone()[0] + 1
            cur.execute(
                """
                INSERT INTO wse_ci_repair_episodes
                    (tenant_id, work_item_id, check_name, failure_signature,
                     fix_commit_sha, occurrence_n, provenance)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                RETURNING id
                """,
                (tenant_id, work_item_id, check_name, failure_signature,
                 fix_commit_sha, occurrence_n, json.dumps(provenance)),
            )
            episode_id = cur.fetchone()[0]
        conn.commit()
        return {"id": episode_id, "occurrence_n": occurrence_n}
    finally:
        conn.close()


def list_ci_repair_episodes(tenant_id: str, failure_signature: str | None = None) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if failure_signature is None:
                cur.execute(
                    "SELECT work_item_id, check_name, failure_signature, fix_commit_sha, occurrence_n, provenance "
                    "FROM wse_ci_repair_episodes WHERE tenant_id = %s ORDER BY id",
                    (tenant_id,),
                )
            else:
                cur.execute(
                    "SELECT work_item_id, check_name, failure_signature, fix_commit_sha, occurrence_n, provenance "
                    "FROM wse_ci_repair_episodes WHERE tenant_id = %s AND failure_signature = %s ORDER BY id",
                    (tenant_id, failure_signature),
                )
            rows = cur.fetchall()
        return [
            {"work_item_id": r[0], "check_name": r[1], "failure_signature": r[2],
             "fix_commit_sha": r[3], "occurrence_n": r[4], "provenance": r[5]}
            for r in rows
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# wse_evidence_publications (WSE-E5-T14 / ADR-26 debounce)
# ---------------------------------------------------------------------------
def get_evidence_publication(work_item_id: str) -> dict[str, Any] | None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT work_item_id, tenant_id, last_commit_sha, fingerprint, published_at "
                "FROM wse_evidence_publications WHERE work_item_id = %s",
                (work_item_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        keys = ["work_item_id", "tenant_id", "last_commit_sha", "fingerprint", "published_at"]
        return dict(zip(keys, row))
    finally:
        conn.close()


def upsert_evidence_publication(
    work_item_id: str, tenant_id: str, last_commit_sha: str, fingerprint: str
) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO wse_evidence_publications
                    (work_item_id, tenant_id, last_commit_sha, fingerprint, published_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (work_item_id) DO UPDATE SET
                    last_commit_sha = EXCLUDED.last_commit_sha,
                    fingerprint = EXCLUDED.fingerprint, published_at = now()
                """,
                (work_item_id, tenant_id, last_commit_sha, fingerprint),
            )
        conn.commit()
    finally:
        conn.close()
