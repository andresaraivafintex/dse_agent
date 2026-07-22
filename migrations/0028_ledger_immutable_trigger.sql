-- Fintex DSE — Plano 08 §F (F3) — ledger imutável por TRIGGER (defesa em
-- profundidade além do REVOKE) + REVOKE de TRUNCATE.
--
-- Hoje audit_log já tem REVOKE UPDATE/DELETE de dse_app/PUBLIC (0001). Isso
-- para a role de app, mas NÃO para um caminho privilegiado (owner/superuser) e
-- NÃO cobre TRUNCATE. Um trigger BEFORE UPDATE OR DELETE que ABORTA é a camada
-- que falta: reforça append-only mesmo se alguém receber privilégio por engano
-- e mesmo contra o próprio dono da tabela (a menos que o trigger seja desligado
-- explicitamente).
--
-- Break-glass (DR/retenção controlada/limpeza de teste): a operação é permitida
-- quando roda como a role de owner/superuser `dse` (a identidade de migração/DR/
-- DBA, NUNCA a de app) OU quando a sessão seta `dse.ledger_maintenance='on'`
-- deliberadamente. A role de app `dse_app` (a superfície de ameaça real — app
-- comprometido / SQL injection) é bloqueada de forma absoluta.
--
-- Alvos: audit_log e model_call_ledger (ambos ledgers append-only). Aditiva e
-- idempotente. retention.py NUNCA toca esses ledgers (recusa alvos audit_log*;
-- anonimiza só ingest_events), então o trigger não interfere no GDPR.

CREATE OR REPLACE FUNCTION dse_ledger_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF current_user = 'dse'
       OR current_setting('dse.ledger_maintenance', true) = 'on' THEN
        RETURN COALESCE(NEW, OLD);  -- break-glass: DR/retenção controlada/teste
    END IF;
    RAISE EXCEPTION
        'ledger % é append-only (plano 08 §F F3): % negado para %',
        TG_TABLE_NAME, TG_OP, current_user
        USING ERRCODE = 'raise_exception',
              HINT = 'ledgers de compliance não sofrem UPDATE/DELETE pela role de app';
END;
$$;

DROP TRIGGER IF EXISTS trg_audit_log_append_only ON audit_log;
CREATE TRIGGER trg_audit_log_append_only
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION dse_ledger_append_only();

DROP TRIGGER IF EXISTS trg_model_call_ledger_append_only ON model_call_ledger;
CREATE TRIGGER trg_model_call_ledger_append_only
    BEFORE UPDATE OR DELETE ON model_call_ledger
    FOR EACH ROW EXECUTE FUNCTION dse_ledger_append_only();

-- REVOKE de TRUNCATE (o REVOKE de UPDATE/DELETE já existe em 0001 p/ audit_log;
-- reforça para ambos os ledgers). TRUNCATE não dispara triggers de linha, então
-- o REVOKE é a única defesa contra ele — daí ser essencial aqui.
REVOKE TRUNCATE ON audit_log FROM dse_app;
REVOKE TRUNCATE ON audit_log FROM PUBLIC;
REVOKE UPDATE, DELETE, TRUNCATE ON model_call_ledger FROM dse_app;
REVOKE UPDATE, DELETE, TRUNCATE ON model_call_ledger FROM PUBLIC;
