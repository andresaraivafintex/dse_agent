"""WSF-E8-T2 — Retention by data classification (§12.2).

Per-tenant/class policy in `tenant_config.retention` (JSONB, reserved migration
`migrations/0018_wsf3.sql`):

    {"internal": {"days": 90}, "restricted": {"days": 30}}

Semantics (deliberately conservative):

- A class with NO entry in the policy => NOTHING is purged for that class.
  Retention is an explicit per-tenant decision — never a silent default.
- `days` counts from `received_at` (ingest) / `created_at` (artifacts).

Targets:

1. ``ingest_events.payload`` (holds the user's `content_snapshot` — FR-06):
   **anonymization** via an UPDATE to a JSONB tombstone that preserves the
   correlation metadata (`kind`, `event_id`, work_item) but removes the
   classified content. A physical DELETE is structurally impossible for the app
   role (`dse_app` only has SELECT/UPDATE on ingest_events —
   0001_foundation.sql) and undesirable (outbox with an FK; the row is evidence
   THAT something arrived; the payload is the classified data). Only touches rows
   with `processed = true` — the dispatcher still needs the unprocessed ones.
2. Store artifacts (WS-E, Phase 3, table being built in parallel): **purge**. The
   target only runs if the table exists AND has the documented minimum columns
   (`tenant_id`, `data_class`, `created_at`) — otherwise it is reported as
   `skipped` with an explicit reason (never silent). The table name is
   configurable (`DSE_ARTIFACT_TABLE`, default ``wse_artifacts``) — to be agreed
   with WS-E at integration time.

audit_log is NEVER a target (append-only, with its own compliance-grade
retention): on top of the schema's structural guarantee (REVOKE UPDATE/DELETE),
this module refuses any target whose name starts with ``audit_log`` (defense in
depth, covered by a test).

P1: every decision here is a timestamp/string comparison in code.
P6: a malformed policy => ValueError at the boundary, never a partial purge.
P8: every run (including a dry-run) writes an audit row via ``dse_audit.emit`` in
    the SAME transaction as the mutation.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
from typing import Any

from dse_audit import emit

from .tenant_config import _get_connection

# Known data classes (same vocabulary as work_items.data_class /
# gateway_contract.data_class). Open by design — a new class needs no migration —
# but the policy shape is validated.
TOMBSTONE_MARKER = "dse_retention_purged"

# Defense in depth: on top of the structural REVOKE in the schema, no code in
# this module accepts an audit ledger target.
_FORBIDDEN_TARGET_PREFIX = "audit_log"

# Real shape of migrations/0017_wse3.sql (WS-E): the data class is NOT in the
# artifacts table — it comes from the work_item that produced the evidence (JOIN).
_ARTIFACT_TABLE = os.environ.get("DSE_ARTIFACT_TABLE", "wse_artifacts")
_ARTIFACT_REQUIRED_COLUMNS = {"tenant_id", "work_item_id", "created_at"}


class RetentionPolicyError(ValueError):
    """Malformed retention policy — clean failure at the boundary (P6)."""


@dataclasses.dataclass(frozen=True)
class RetentionPolicy:
    data_class: str
    days: int


@dataclasses.dataclass
class TargetResult:
    target: str            # 'ingest_events' | artifacts table | ...
    data_class: str
    action: str            # 'anonymize' | 'purge' | 'skipped'
    cutoff: dt.datetime | None
    candidates: int
    affected: int
    reason: str | None = None   # filled in when action == 'skipped'
    # bucket/store_key of the deleted artifacts — cleaning up the corresponding
    # S3 object (Garage) belongs to WS-E's lifecycle; the list goes into the
    # audit row so the compensating cleanup is never silent.
    purged_store_keys: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class RetentionReport:
    tenant_id: str
    dry_run: bool
    results: list[TargetResult] = dataclasses.field(default_factory=list)

    @property
    def total_affected(self) -> int:
        return sum(r.affected for r in self.results)


# ---------------------------------------------------------------------------
# Policy (read/write on tenant_config.retention)
# ---------------------------------------------------------------------------
def _parse_policies(raw: Any) -> dict[str, RetentionPolicy]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise RetentionPolicyError(f"tenant_config.retention must be a JSON object, got {type(raw).__name__}")
    policies: dict[str, RetentionPolicy] = {}
    for data_class, spec in raw.items():
        if not isinstance(spec, dict) or "days" not in spec:
            raise RetentionPolicyError(
                f"policy for class '{data_class}' must have shape {{\"days\": int}}, got: {spec!r}"
            )
        days = spec["days"]
        if not isinstance(days, int) or isinstance(days, bool) or days <= 0:
            raise RetentionPolicyError(
                f"policy for class '{data_class}': days must be an int > 0, got: {days!r}"
            )
        policies[data_class] = RetentionPolicy(data_class=data_class, days=days)
    return policies


def get_retention_policies(tenant_id: str, conn=None) -> dict[str, RetentionPolicy]:
    """The tenant's effective policy per data_class. An empty dict = no retention
    configured = nothing is purged (conservative).

    Cross-workstream consumption contract: the artifact store lifecycle job
    (WS-E) must read the policy FROM HERE (single source), never duplicate it.
    """
    owns_conn = conn is None
    if owns_conn:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT retention FROM tenant_config WHERE tenant_id = %s", (tenant_id,))
            row = cur.fetchone()
    finally:
        if owns_conn:
            conn.close()
    return _parse_policies(row[0] if row else None)


def set_retention_policy(
    tenant_id: str,
    data_class: str,
    *,
    days: int | None,
    actor: str,
    conn=None,
) -> dict[str, RetentionPolicy]:
    """Sets (days=int) or removes (days=None) a class's policy. Validates the
    shape BEFORE writing and audits the change (P8) in the same transaction.
    Creates the tenant_config row if it does not exist yet."""
    if days is not None and (not isinstance(days, int) or isinstance(days, bool) or days <= 0):
        raise RetentionPolicyError(f"days must be an int > 0 or None (to remove), got: {days!r}")

    owns_conn = conn is None
    if owns_conn:
        conn = _get_connection()
    try:
        with conn.cursor() as cur:
            if days is None:
                cur.execute(
                    """
                    INSERT INTO tenant_config (tenant_id) VALUES (%s)
                    ON CONFLICT (tenant_id) DO UPDATE SET retention = tenant_config.retention - %s
                    """,
                    (tenant_id, data_class),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO tenant_config (tenant_id, retention)
                    VALUES (%s, jsonb_build_object(%s, jsonb_build_object('days', %s::int)))
                    ON CONFLICT (tenant_id) DO UPDATE SET
                        retention = tenant_config.retention
                                    || jsonb_build_object(%s, jsonb_build_object('days', %s::int))
                    """,
                    (tenant_id, data_class, days, data_class, days),
                )
        emit(
            actor=actor,
            action="retention_policy_set" if days is not None else "retention_policy_removed",
            tenant_id=tenant_id,
            details={"data_class": data_class, "days": days},
            conn=conn,
        )
        if owns_conn:
            conn.commit()
    except Exception:
        if owns_conn:
            conn.rollback()
        raise
    finally:
        if owns_conn:
            conn.close()
    return get_retention_policies(tenant_id)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def _assert_target_allowed(table: str) -> None:
    if table.strip().lower().startswith(_FORBIDDEN_TARGET_PREFIX):
        raise RetentionPolicyError(
            "audit_log is NEVER a target of this retention job — the ledger is append-only "
            "with its own compliance-grade retention (WSF-E8-T2/§12.2)."
        )


def _tombstone(policy: RetentionPolicy, executed_at: dt.datetime) -> dict[str, Any]:
    return {
        TOMBSTONE_MARKER: True,
        "data_class": policy.data_class,
        "retention_days": policy.days,
        "purged_at": executed_at.isoformat(),
    }


def _run_ingest_events_target(
    cur,
    tenant_id: str,
    policy: RetentionPolicy,
    cutoff: dt.datetime,
    executed_at: dt.datetime,
    dry_run: bool,
) -> TargetResult:
    _assert_target_allowed("ingest_events")
    # Candidates: events ALREADY processed, older than the cutoff, of the
    # corresponding work_item's class, not yet tombstoned (idempotency).
    cur.execute(
        """
        SELECT count(*) FROM ingest_events e
        JOIN work_items w ON w.id = e.work_item_id
        WHERE w.tenant_id = %s
          AND w.data_class = %s
          AND e.processed
          AND e.received_at < %s
          AND NOT (e.payload ? %s)
        """,
        (tenant_id, policy.data_class, cutoff, TOMBSTONE_MARKER),
    )
    candidates = cur.fetchone()[0]
    affected = 0
    if not dry_run and candidates:
        cur.execute(
            """
            UPDATE ingest_events e
            SET payload = %s::jsonb
            FROM work_items w
            WHERE w.id = e.work_item_id
              AND w.tenant_id = %s
              AND w.data_class = %s
              AND e.processed
              AND e.received_at < %s
              AND NOT (e.payload ? %s)
            """,
            (json.dumps(_tombstone(policy, executed_at)), tenant_id, policy.data_class, cutoff, TOMBSTONE_MARKER),
        )
        affected = cur.rowcount
    return TargetResult(
        target="ingest_events",
        data_class=policy.data_class,
        action="anonymize",
        cutoff=cutoff,
        candidates=candidates,
        affected=affected,
    )


def _artifact_table_shape(cur, table: str) -> set[str]:
    cur.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s",
        (table,),
    )
    return {r[0] for r in cur.fetchall()}


def _run_artifacts_target(
    cur,
    tenant_id: str,
    policy: RetentionPolicy,
    cutoff: dt.datetime,
    dry_run: bool,
    table: str,
) -> TargetResult:
    _assert_target_allowed(table)
    columns = _artifact_table_shape(cur, table)
    if not columns:
        return TargetResult(
            target=table, data_class=policy.data_class, action="skipped", cutoff=cutoff,
            candidates=0, affected=0,
            reason=f"table '{table}' does not exist (WS-E artifact store not integrated yet)",
        )
    missing = _ARTIFACT_REQUIRED_COLUMNS - columns
    if missing:
        return TargetResult(
            target=table, data_class=policy.data_class, action="skipped", cutoff=cutoff,
            candidates=0, affected=0,
            reason=f"table '{table}' missing required columns {sorted(missing)} — agree the shape with WS-E",
        )
    # psycopg2 does not parameterize identifiers — the name comes from env/an
    # internal parameter, already validated against the forbidden prefix; even
    # so, quote it.
    from psycopg2 import sql as _sql

    # QUARANTINED artifacts are never purged by retention: quarantine is evidence
    # held for investigation (WSE-E5-T12 / WS-F Phase 2) — it is only released by
    # an operator decision, not by an age policy.
    quarantine_guard = (
        _sql.SQL(" AND a.quarantined_at IS NULL") if "quarantined_at" in columns else _sql.SQL("")
    )
    where = _sql.SQL(
        "FROM {t} a JOIN work_items w ON w.id = a.work_item_id "
        "WHERE a.tenant_id = %s AND w.data_class = %s AND a.created_at < %s"
    ).format(t=_sql.Identifier(table)) + quarantine_guard

    cur.execute(_sql.SQL("SELECT count(*) ") + where, (tenant_id, policy.data_class, cutoff))
    candidates = cur.fetchone()[0]

    if not dry_run:
        # Privilege preflight (avoids aborting the transaction halfway — P6):
        # WS-E's migration (0017) does not grant DELETE on wse_artifacts to the
        # app role. Until that grant is agreed at integration, the real purge
        # stays explicitly skipped (dry-run/counting works with SELECT alone).
        cur.execute("SELECT has_table_privilege(current_user, %s, 'DELETE')", (table,))
        if not cur.fetchone()[0]:
            return TargetResult(
                target=table, data_class=policy.data_class, action="skipped", cutoff=cutoff,
                candidates=candidates, affected=0,
                reason=(
                    f"current role has no GRANT DELETE on '{table}' (0017_wse3.sql) — "
                    "request logged in the WS-F README for WS-E to grant at integration"
                ),
            )

    affected = 0
    purged_keys: list[str] = []
    if not dry_run and candidates:
        # Capture bucket/store_key BEFORE the DELETE: the corresponding S3 object
        # (Garage) must be removed by WS-E's lifecycle — the list goes into the
        # audit row so the compensating cleanup is never silent.
        returning = (
            _sql.SQL(" RETURNING a.bucket || '/' || a.store_key")
            if {"bucket", "store_key"} <= columns
            else _sql.SQL(" RETURNING a.work_item_id")
        )
        cur.execute(
            _sql.SQL("DELETE FROM {t} a USING work_items w "
                     "WHERE w.id = a.work_item_id AND a.tenant_id = %s AND w.data_class = %s "
                     "AND a.created_at < %s").format(t=_sql.Identifier(table))
            + quarantine_guard + returning,
            (tenant_id, policy.data_class, cutoff),
        )
        purged_keys = [r[0] for r in cur.fetchall()]
        affected = len(purged_keys)
    return TargetResult(
        target=table, data_class=policy.data_class, action="purge", cutoff=cutoff,
        candidates=candidates, affected=affected, purged_store_keys=purged_keys,
    )


def run_retention(
    tenant_id: str,
    *,
    dry_run: bool = False,
    actor: str = "system:retention-job",
    now: dt.datetime | None = None,
    artifact_table: str | None = None,
    conn=None,
) -> RetentionReport:
    """Runs (or simulates, with ``dry_run=True``) retention for ONE tenant.

    A single transaction: mutations + the audit row are atomic (P8). With no
    policy configured => a no-op audited as zero targets (never an implicit
    purge)."""
    executed_at = now or dt.datetime.now(dt.timezone.utc)
    table = artifact_table or _ARTIFACT_TABLE
    report = RetentionReport(tenant_id=tenant_id, dry_run=dry_run)

    owns_conn = conn is None
    if owns_conn:
        conn = _get_connection()
    try:
        policies = get_retention_policies(tenant_id, conn=conn)
        with conn.cursor() as cur:
            for policy in policies.values():
                cutoff = executed_at - dt.timedelta(days=policy.days)
                report.results.append(
                    _run_ingest_events_target(cur, tenant_id, policy, cutoff, executed_at, dry_run)
                )
                report.results.append(
                    _run_artifacts_target(cur, tenant_id, policy, cutoff, dry_run, table)
                )
        emit(
            actor=actor,
            action="retention_dry_run" if dry_run else "retention_executed",
            tenant_id=tenant_id,
            details={
                "executed_at": executed_at.isoformat(),
                "policies": {p.data_class: p.days for p in policies.values()},
                "results": [
                    {
                        "target": r.target,
                        "data_class": r.data_class,
                        "action": r.action,
                        "cutoff": r.cutoff.isoformat() if r.cutoff else None,
                        "candidates": r.candidates,
                        "affected": r.affected,
                        "reason": r.reason,
                        "purged_store_keys": r.purged_store_keys,
                    }
                    for r in report.results
                ],
            },
            conn=conn,
        )
        if owns_conn:
            conn.commit()
    except Exception:
        if owns_conn:
            conn.rollback()
        raise
    finally:
        if owns_conn:
            conn.close()
    return report


def run_retention_all_tenants(
    *,
    dry_run: bool = False,
    actor: str = "system:retention-job",
    now: dt.datetime | None = None,
) -> list[RetentionReport]:
    """Sweeps every tenant that has a policy configured (scheduled job).

    A failure on ONE tenant (e.g. a corrupted policy) does NOT abort the sweep of
    the others — that tenant simply gets no purge (clean rollback, P6), the
    failure is audited (`retention_failed`, P8) and the sweep continues. Real
    finding: the first version aborted the whole sweep on the first tenant with
    malformed JSONB."""
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT tenant_id FROM tenant_config WHERE retention <> '{}'::jsonb ORDER BY tenant_id")
            tenant_ids = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()

    reports: list[RetentionReport] = []
    for tenant_id in tenant_ids:
        try:
            reports.append(run_retention(tenant_id, dry_run=dry_run, actor=actor, now=now))
        except Exception as exc:  # noqa: BLE001 — per-tenant isolation; the failure is audited, never swallowed
            emit(
                actor=actor,
                action="retention_failed",
                tenant_id=tenant_id,
                details={"error": str(exc), "dry_run": dry_run},
            )
    return reports
