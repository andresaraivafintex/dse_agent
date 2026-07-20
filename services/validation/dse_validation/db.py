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
