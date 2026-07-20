"""WSD-E2-T1: motor de política per-stage/per-tenant enforçado no call time.

Testes REAIS contra Postgres + LiteLLM/echo reais. Provam:
  - resolução por especificidade/coringa e default permissivo;
  - deny tipado (GatewayErrorResponse{policy_denied}) numa chamada a modelo não
    permitido, com linha de audit (P8);
  - hot-reload (mudar a linha da tabela muda a decisão sem redeploy);
  - carregar política declarativa de um arquivo YAML.
"""
from __future__ import annotations

import os
import tempfile

import pytest
from dse_audit.client import get_connection as audit_conn
from dse_contracts.gateway_contract import GatewayCallHeaders, Stage
from model_gateway_client import (
    GatewayCallError,
    chat_completion,
    load_policies_from_file,
    mint_virtual_key,
    resolve_policy,
    revoke_virtual_key,
)
from model_gateway_client import db, policy

ECHO = "eco/echo-model"


def _insert_policy(tenant_id, stage, allowed_models, *, data_class="*", risk_class="*", preferred=None, priority=0):
    import json

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO model_policies
                    (tenant_id, stage, data_class, risk_class, allowed_models, preferred_model, priority)
                VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s)
                ON CONFLICT (tenant_id, stage, data_class, risk_class)
                DO UPDATE SET allowed_models=EXCLUDED.allowed_models,
                              preferred_model=EXCLUDED.preferred_model,
                              priority=EXCLUDED.priority, is_active=true
                """,
                (tenant_id, stage, data_class, risk_class, json.dumps(allowed_models), preferred, priority),
            )
        conn.commit()
    finally:
        conn.close()
    policy.clear_cache()


def _audit_actions(work_item_id):
    conn = audit_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT action FROM audit_log WHERE work_item_id=%s ORDER BY id", (work_item_id,)
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def test_no_policy_is_permissive(unique_ids):
    d = resolve_policy(unique_ids["tenant_id"], "coder")
    assert d.allowed_models is None
    assert d.permits("anything/at-all")


def test_specificity_beats_wildcard(unique_ids):
    t = unique_ids["tenant_id"]
    # Coringa de STAGE dentro do MESMO tenant (nunca um tenant '*' global — isso
    # poluiria a decisão de todos os outros testes/tenants no Postgres real).
    _insert_policy(t, "*", ["fallback/model"])
    _insert_policy(t, "coder", ["strong/model"], preferred="strong/model", priority=0)
    d = resolve_policy(t, "coder")
    assert d.allowed_models == ["strong/model"]
    assert d.preferred_model == "strong/model"
    # stage não coberto por linha específica cai no coringa de stage do tenant
    d2 = resolve_policy(t, "reviewer")
    assert d2.allowed_models == ["fallback/model"]


def test_call_to_disallowed_model_is_denied_and_audited(unique_ids):
    t, wi = unique_ids["tenant_id"], unique_ids["work_item_id"]
    # política só permite um modelo que NÃO é o echo
    _insert_policy(t, "coder", ["bedrock/anthropic.claude-3-5-sonnet"])
    key = mint_virtual_key(t, wi, Stage.coder, models=[ECHO])
    headers = GatewayCallHeaders(tenant_id=t, work_item_id=wi, stage=Stage.coder)
    try:
        with pytest.raises(GatewayCallError) as ei:
            chat_completion(headers=headers, virtual_key=key, model=ECHO, messages=[{"role": "user", "content": "x"}])
        assert ei.value.status_code == 403
        assert ei.value.body["error"] == "policy_denied"
        assert ei.value.body["kind"] == "model_not_allowed"
        assert "gateway.call_denied_policy" in _audit_actions(wi)
    finally:
        revoke_virtual_key(key)


def test_allowed_model_passes_through(unique_ids):
    t, wi = unique_ids["tenant_id"], unique_ids["work_item_id"]
    _insert_policy(t, "coder", [ECHO], preferred=ECHO)
    key = mint_virtual_key(t, wi, Stage.coder, models=[ECHO])
    headers = GatewayCallHeaders(tenant_id=t, work_item_id=wi, stage=Stage.coder)
    try:
        r = chat_completion(headers=headers, virtual_key=key, model=ECHO, messages=[{"role": "user", "content": "abc"}])
        assert r.content == "ECHO[cba]"
    finally:
        revoke_virtual_key(key)


def test_hot_reload_changes_decision_without_redeploy(unique_ids):
    t, wi = unique_ids["tenant_id"], unique_ids["work_item_id"]
    _insert_policy(t, "coder", ["other/model"])  # nega echo
    key = mint_virtual_key(t, wi, Stage.coder, models=[ECHO])
    headers = GatewayCallHeaders(tenant_id=t, work_item_id=wi, stage=Stage.coder)
    try:
        with pytest.raises(GatewayCallError):
            chat_completion(headers=headers, virtual_key=key, model=ECHO, messages=[{"role": "user", "content": "x"}])
        # operador atualiza a política (mesma linha, mesmo processo, sem redeploy)
        _insert_policy(t, "coder", [ECHO])  # clear_cache dentro do helper simula TTL
        r = chat_completion(headers=headers, virtual_key=key, model=ECHO, messages=[{"role": "user", "content": "x"}])
        assert r.content.startswith("ECHO[")
    finally:
        revoke_virtual_key(key)


def test_load_policies_from_yaml_file(unique_ids):
    t = unique_ids["tenant_id"]
    doc = f"""
policies:
  - tenant_id: "{t}"
    stage: "reviewer"
    allowed_models: ["cheap/model"]
    preferred_model: "cheap/model"
    priority: 5
"""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(doc)
        path = fh.name
    try:
        n = load_policies_from_file(path)
        assert n == 1
        d = resolve_policy(t, "reviewer")
        assert d.allowed_models == ["cheap/model"]
        assert d.preferred_model == "cheap/model"
    finally:
        os.unlink(path)
