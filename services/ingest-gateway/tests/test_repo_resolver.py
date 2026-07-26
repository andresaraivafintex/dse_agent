"""Repo resolution cascade (Report 07 C2 / Phase B).

Proves the precedence order and — crucially — that ambiguity/absence returns
(None, None) so the workflow ASKS (never guesses). Runs against the real
Postgres (the standard for these suites); uses an isolated synthetic tenant.
"""
from __future__ import annotations

import uuid

import psycopg2
import pytest

from ingest_gateway.repo_resolver import parse_explicit_repo, resolve_repo

DSN = "postgresql://dse_app:dse_app_dev_only@localhost:5432/dse"
SUPER_DSN = DSN.replace("dse_app:dse_app_dev_only", "dse:dse_dev_only")


@pytest.fixture()
def tenant():
    tid = f"crm-repo-{uuid.uuid4().hex[:8]}"
    yield tid
    conn = psycopg2.connect(SUPER_DSN)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM repo_bindings WHERE tenant_id = %s", (tid,))
    conn.commit()
    conn.close()


def _bind(tenant, platform, btype, value, repo, branch="main"):
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO repo_bindings (tenant_id, platform, binding_type, binding_value, repo, base_branch) "
            "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (tenant, platform, btype, value, repo, branch),
        )
    conn.commit()
    conn.close()


def test_parse_explicit_repo():
    assert parse_explicit_repo("faz X no repo=org/x branch=dev") == ("org/x", "dev")
    assert parse_explicit_repo("nothing here") == (None, None)


def test_rung1_explicit_override_beats_binding(tenant):
    _bind(tenant, "slack", "channel", "C1", "org/from-binding")
    conn = psycopg2.connect(DSN)
    try:
        repo, branch = resolve_repo(
            conn, tenant_id=tenant, platform="slack",
            signals={"text": "usa repo=org/explicit", "channel": "C1"},
        )
    finally:
        conn.close()
    assert repo == "org/explicit"  # explicit beats the channel binding


def test_rung2_channel_binding(tenant):
    _bind(tenant, "slack", "channel", "C_PAY", "org/payments", "develop")
    conn = psycopg2.connect(DSN)
    try:
        repo, branch = resolve_repo(
            conn, tenant_id=tenant, platform="slack",
            signals={"text": "conserta o saldo", "channel": "C_PAY"},
        )
    finally:
        conn.close()
    assert (repo, branch) == ("org/payments", "develop")


def test_component_beats_project_jira(tenant):
    _bind(tenant, "jira", "project", "FINX", "org/mono")
    _bind(tenant, "jira", "component", "Payments", "org/payments")
    conn = psycopg2.connect(DSN)
    try:
        repo, _ = resolve_repo(
            conn, tenant_id=tenant, platform="jira",
            signals={"text": "bug", "component": "Payments", "project": "FINX"},
        )
    finally:
        conn.close()
    assert repo == "org/payments"  # component (finer) beats project


def test_rung4_single_repo_default(tenant):
    # tenant with ONE distinct repo across any binding -> default with no signal
    _bind(tenant, "slack", "channel", "C_A", "org/only")
    conn = psycopg2.connect(DSN)
    try:
        repo, _ = resolve_repo(
            conn, tenant_id=tenant, platform="slack",
            signals={"text": "algo", "channel": "C_DESCONHECIDO"},
        )
    finally:
        conn.close()
    assert repo == "org/only"


def test_ambiguous_returns_none_for_clarification(tenant):
    # two distinct repos, no signal matches -> does NOT guess
    _bind(tenant, "slack", "channel", "C_A", "org/a")
    _bind(tenant, "slack", "channel", "C_B", "org/b")
    conn = psycopg2.connect(DSN)
    try:
        repo, branch = resolve_repo(
            conn, tenant_id=tenant, platform="slack",
            signals={"text": "algo", "channel": "C_X"},
        )
    finally:
        conn.close()
    assert (repo, branch) == (None, None)


def test_empty_tenant_returns_none(tenant):
    conn = psycopg2.connect(DSN)
    try:
        repo, branch = resolve_repo(
            conn, tenant_id=tenant, platform="slack",
            signals={"text": "no binding at all", "channel": "C_X"},
        )
    finally:
        conn.close()
    assert (repo, branch) == (None, None)
