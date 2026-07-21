"""WSF-E8-T2 — retenção por classificação de dados, contra o Postgres REAL
da fundação (nunca mockado). Cobre: política por tenant/classe, anonimização
de ingest_events.payload, expurgo de artifacts (wse_artifacts, WS-E), modo
dry-run, idempotência, e as garantias de que audit_log NUNCA é alvo.
"""
from __future__ import annotations

import datetime as dt
import json
import uuid

import psycopg2
import psycopg2.extras
import pytest
from dse_platform import (
    RetentionPolicyError,
    get_retention_policies,
    run_retention,
    set_retention_policy,
    upsert_tenant_config,
)
from dse_platform.retention import TOMBSTONE_MARKER, _assert_target_allowed

SU_DSN = "postgresql://dse:dse_dev_only@localhost:5432/dse"
APP_DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"

NOW = dt.datetime.now(dt.timezone.utc)
OLD = NOW - dt.timedelta(days=120)
FRESH = NOW - dt.timedelta(days=1)


@pytest.fixture()
def tenant_id():
    return f"ret-{uuid.uuid4().hex[:8]}"


def _su():
    return psycopg2.connect(SU_DSN)


def _mk_work_item(cur, tenant_id: str, data_class: str) -> str:
    wid = f"wi-{uuid.uuid4().hex[:10]}"
    cur.execute(
        """
        INSERT INTO work_items (id, tenant_id, source, source_ref, requester, data_class, idempotency_key)
        VALUES (%s, %s, 'slack', '{}'::jsonb, 'usr_test', %s, %s)
        """,
        (wid, tenant_id, data_class, f"idem-{wid}"),
    )
    return wid


def _mk_event(cur, wid: str, *, processed: bool, received_at: dt.datetime, content: str) -> str:
    eid = f"ev-{uuid.uuid4().hex[:12]}"
    cur.execute(
        """
        INSERT INTO ingest_events (work_item_id, event_id, kind, payload, processed, received_at)
        VALUES (%s, %s, 'task_request', %s::jsonb, %s, %s)
        """,
        (wid, eid, json.dumps({"content_snapshot": content, "kind": "task_request"}), processed, received_at),
    )
    return eid


def _event_payload(event_id: str) -> dict:
    conn = _su()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT payload FROM ingest_events WHERE event_id = %s", (event_id,))
            return cur.fetchone()[0]
    finally:
        conn.close()


def _audit_actions(tenant_id: str) -> list[str]:
    conn = _su()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT action FROM audit_log WHERE tenant_id = %s ORDER BY ts, id", (tenant_id,))
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Política
# ---------------------------------------------------------------------------
def test_policy_set_get_remove_and_audit(tenant_id):
    assert get_retention_policies(tenant_id) == {}

    policies = set_retention_policy(tenant_id, "internal", days=90, actor="usr_admin")
    assert policies["internal"].days == 90

    policies = set_retention_policy(tenant_id, "restricted", days=30, actor="usr_admin")
    assert set(policies) == {"internal", "restricted"}

    policies = set_retention_policy(tenant_id, "restricted", days=None, actor="usr_admin")
    assert set(policies) == {"internal"}

    actions = _audit_actions(tenant_id)
    assert actions.count("retention_policy_set") == 2
    assert actions.count("retention_policy_removed") == 1


@pytest.mark.parametrize("bad_days", [0, -1, 1.5, True, "30"])
def test_policy_rejects_malformed_days(tenant_id, bad_days):
    with pytest.raises(RetentionPolicyError):
        set_retention_policy(tenant_id, "internal", days=bad_days, actor="usr_admin")


def test_malformed_stored_policy_fails_clean_not_partial(tenant_id):
    """P6: JSONB corrompido => erro na borda, nunca um expurgo 'melhor esforço'.
    E a varredura multi-tenant NÃO pode ser abortada por UM tenant quebrado
    (achado real: a 1ª execução do job agendado no container morreu no
    resíduo malformado deste próprio teste)."""
    from dse_platform import run_retention_all_tenants

    upsert_tenant_config(tenant_id)
    conn = _su()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tenant_config SET retention = '{\"internal\": {\"dias\": 90}}'::jsonb WHERE tenant_id = %s",
                (tenant_id,),
            )
        conn.commit()

        with pytest.raises(RetentionPolicyError):
            run_retention(tenant_id)

        # a sweep continua apesar do tenant quebrado, e a falha é auditada
        run_retention_all_tenants()
        assert "retention_failed" in _audit_actions(tenant_id)
    finally:
        # limpa o resíduo malformado (não deixar lixo para o job agendado real)
        conn2 = _su()
        with conn2.cursor() as cur:
            cur.execute(
                "UPDATE tenant_config SET retention = '{}'::jsonb WHERE tenant_id = %s", (tenant_id,)
            )
        conn2.commit()
        conn2.close()
        conn.close()


# ---------------------------------------------------------------------------
# ingest_events — anonimização
# ---------------------------------------------------------------------------
def test_no_policy_means_no_purge(tenant_id):
    conn = _su()
    try:
        with conn.cursor() as cur:
            wid = _mk_work_item(cur, tenant_id, "internal")
            old_ev = _mk_event(cur, wid, processed=True, received_at=OLD, content="segredo antigo")
        conn.commit()
    finally:
        conn.close()

    report = run_retention(tenant_id)

    assert report.total_affected == 0
    assert report.results == []  # nenhuma classe configurada => nenhum alvo
    assert _event_payload(old_ev)["content_snapshot"] == "segredo antigo"
    assert "retention_executed" in _audit_actions(tenant_id)  # execução auditada mesmo no-op


def test_anonymizes_only_processed_old_events_of_the_class(tenant_id):
    conn = _su()
    try:
        with conn.cursor() as cur:
            wid_int = _mk_work_item(cur, tenant_id, "internal")
            wid_res = _mk_work_item(cur, tenant_id, "restricted")
            ev_old_processed = _mk_event(cur, wid_int, processed=True, received_at=OLD, content="EXPURGAR")
            ev_old_unprocessed = _mk_event(cur, wid_int, processed=False, received_at=OLD, content="outbox vivo")
            ev_fresh = _mk_event(cur, wid_int, processed=True, received_at=FRESH, content="recente")
            ev_other_class = _mk_event(cur, wid_res, processed=True, received_at=OLD, content="classe sem política")
        conn.commit()
    finally:
        conn.close()

    set_retention_policy(tenant_id, "internal", days=90, actor="usr_admin")
    report = run_retention(tenant_id)

    ingest_results = [r for r in report.results if r.target == "ingest_events"]
    assert len(ingest_results) == 1
    assert ingest_results[0].action == "anonymize"
    assert ingest_results[0].affected == 1

    purged = _event_payload(ev_old_processed)
    assert purged[TOMBSTONE_MARKER] is True
    assert purged["data_class"] == "internal"
    assert "EXPURGAR" not in json.dumps(purged)

    assert _event_payload(ev_old_unprocessed)["content_snapshot"] == "outbox vivo"
    assert _event_payload(ev_fresh)["content_snapshot"] == "recente"
    assert _event_payload(ev_other_class)["content_snapshot"] == "classe sem política"


def test_run_is_idempotent(tenant_id):
    conn = _su()
    try:
        with conn.cursor() as cur:
            wid = _mk_work_item(cur, tenant_id, "internal")
            _mk_event(cur, wid, processed=True, received_at=OLD, content="uma vez só")
        conn.commit()
    finally:
        conn.close()

    set_retention_policy(tenant_id, "internal", days=90, actor="usr_admin")
    first = run_retention(tenant_id)
    second = run_retention(tenant_id)

    assert first.total_affected == 1
    assert second.total_affected == 0  # tombstone não é re-expurgado


def test_dry_run_counts_but_never_mutates(tenant_id):
    conn = _su()
    try:
        with conn.cursor() as cur:
            wid = _mk_work_item(cur, tenant_id, "internal")
            ev = _mk_event(cur, wid, processed=True, received_at=OLD, content="ainda aqui")
        conn.commit()
    finally:
        conn.close()

    set_retention_policy(tenant_id, "internal", days=90, actor="usr_admin")
    report = run_retention(tenant_id, dry_run=True)

    ingest = [r for r in report.results if r.target == "ingest_events"][0]
    assert ingest.candidates == 1
    assert ingest.affected == 0
    assert _event_payload(ev)["content_snapshot"] == "ainda aqui"
    assert "retention_dry_run" in _audit_actions(tenant_id)
    assert "retention_executed" not in _audit_actions(tenant_id)


# ---------------------------------------------------------------------------
# audit_log NUNCA é alvo
# ---------------------------------------------------------------------------
def test_audit_log_is_refused_as_target():
    with pytest.raises(RetentionPolicyError, match="append-only"):
        _assert_target_allowed("audit_log")
    with pytest.raises(RetentionPolicyError, match="append-only"):
        _assert_target_allowed("audit_log_default")


def test_run_with_audit_log_as_artifact_table_is_refused(tenant_id):
    set_retention_policy(tenant_id, "internal", days=90, actor="usr_admin")
    with pytest.raises(RetentionPolicyError, match="append-only"):
        run_retention(tenant_id, artifact_table="audit_log")


def test_audit_rows_survive_retention(tenant_id):
    """Linhas de audit mais velhas que qualquer política permanecem intactas
    (a retenção do ledger é compliance-grade própria — §12.2)."""
    conn = _su()
    try:
        with conn.cursor() as cur:
            wid = _mk_work_item(cur, tenant_id, "internal")
            _mk_event(cur, wid, processed=True, received_at=OLD, content="x")
            # linha de audit antiga do mesmo tenant (inserida como superuser
            # com ts antigo — audit_log não tem default de imutabilidade de ts)
            cur.execute(
                "INSERT INTO audit_log (ts, work_item_id, tenant_id, actor, action, details) "
                "VALUES (%s, %s, %s, 'usr_x', 'admitted', '{}'::jsonb)",
                (OLD, wid, tenant_id),
            )
        conn.commit()
    finally:
        conn.close()

    set_retention_policy(tenant_id, "internal", days=90, actor="usr_admin")
    before = _audit_actions(tenant_id)
    run_retention(tenant_id)
    after = _audit_actions(tenant_id)

    assert "admitted" in after  # a linha antiga sobreviveu
    assert len(after) == len(before) + 1  # só CRESCEU (a execução auditada)


# ---------------------------------------------------------------------------
# Artifacts (wse_artifacts, WS-E — shape real da 0017)
# ---------------------------------------------------------------------------
def _mk_artifact(cur, wid: str, tenant_id: str, *, created_at: dt.datetime, quarantined: bool = False) -> str:
    key = f"demo/{uuid.uuid4().hex[:10]}.mp4"
    cur.execute(
        """
        INSERT INTO wse_artifacts (work_item_id, tenant_id, kind, bucket, store_key,
                                   ttl_seconds, expires_at, created_at, quarantined_at)
        VALUES (%s, %s, 'demo_video', %s, %s, 900, %s, %s, %s)
        """,
        (
            wid, tenant_id, f"dse-{tenant_id}", key,
            created_at + dt.timedelta(seconds=900), created_at,
            NOW if quarantined else None,
        ),
    )
    return key


def _artifact_keys(tenant_id: str) -> set[str]:
    conn = _su()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT store_key FROM wse_artifacts WHERE tenant_id = %s", (tenant_id,))
            return {r[0] for r in cur.fetchall()}
    finally:
        conn.close()


def test_artifact_purge_skipped_without_delete_grant(tenant_id):
    """A role de app (dse_app) não tem DELETE em wse_artifacts (0017) — o
    expurgo real reporta skipped com razão explícita, nunca aborta nem
    silencia; a CONTAGEM de candidatos funciona (só exige SELECT)."""
    conn = _su()
    try:
        with conn.cursor() as cur:
            wid = _mk_work_item(cur, tenant_id, "internal")
            _mk_artifact(cur, wid, tenant_id, created_at=OLD)
        conn.commit()
    finally:
        conn.close()

    set_retention_policy(tenant_id, "internal", days=90, actor="usr_admin")
    report = run_retention(tenant_id)  # conexão default = dse_app

    art = [r for r in report.results if r.target == "wse_artifacts"][0]
    assert art.action == "skipped"
    assert art.candidates == 1
    assert "GRANT DELETE" in art.reason
    assert _artifact_keys(tenant_id)  # nada deletado


def test_artifact_purge_deletes_old_keeps_fresh_and_quarantined(tenant_id):
    """Com uma conexão privilegiada (o grant pedido ao WS-E), o expurgo real:
    deleta artifacts velhos da classe, preserva os recentes e NUNCA toca
    artifacts quarentenados (evidência retida para investigação)."""
    su_conn = _su()
    try:
        with su_conn.cursor() as cur:
            wid = _mk_work_item(cur, tenant_id, "internal")
            old_key = _mk_artifact(cur, wid, tenant_id, created_at=OLD)
            fresh_key = _mk_artifact(cur, wid, tenant_id, created_at=FRESH)
            quarantined_key = _mk_artifact(cur, wid, tenant_id, created_at=OLD, quarantined=True)
        su_conn.commit()

        set_retention_policy(tenant_id, "internal", days=90, actor="usr_admin")
        report = run_retention(tenant_id, conn=su_conn)
        su_conn.commit()
    finally:
        su_conn.close()

    art = [r for r in report.results if r.target == "wse_artifacts"][0]
    assert art.action == "purge"
    assert art.affected == 1
    assert art.purged_store_keys == [f"dse-{tenant_id}/{old_key}"]

    remaining = _artifact_keys(tenant_id)
    assert old_key not in remaining
    assert fresh_key in remaining
    assert quarantined_key in remaining

    # o audit row carrega as chaves deletadas (cleanup compensatório no
    # Garage pelo lifecycle do WS-E — nunca silencioso)
    conn = _su()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT details FROM audit_log WHERE tenant_id = %s AND action = 'retention_executed' "
                "ORDER BY ts DESC, id DESC LIMIT 1",
                (tenant_id,),
            )
            details = cur.fetchone()["details"]
    finally:
        conn.close()
    purge_results = [r for r in details["results"] if r["target"] == "wse_artifacts"]
    assert purge_results[0]["purged_store_keys"] == [f"dse-{tenant_id}/{old_key}"]
