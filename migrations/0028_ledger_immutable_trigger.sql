-- Fintex DSE — Plan 08 §F (F3) — immutable ledger enforced by TRIGGER (defense
-- in depth beyond the REVOKE) + REVOKE of TRUNCATE.
--
-- Today audit_log already has REVOKE UPDATE/DELETE from dse_app/PUBLIC (0001).
-- That stops the app role, but NOT a privileged path (owner/superuser) and it
-- does NOT cover TRUNCATE. A BEFORE UPDATE OR DELETE trigger that ABORTS is the
-- missing layer: it enforces append-only even if someone is granted a privilege
-- by mistake, and even against the table owner itself (unless the trigger is
-- disabled explicitly).
--
-- Break-glass (DR/controlled retention/test cleanup): the operation is allowed
-- when running as the owner/superuser role `dse` (the migration/DR/DBA identity,
-- NEVER the app one) OR when the session deliberately sets
-- `dse.ledger_maintenance='on'`. The app role `dse_app` (the real threat surface
-- — compromised app / SQL injection) is blocked absolutely.
--
-- Targets: audit_log and model_call_ledger (both append-only ledgers). Additive
-- and idempotent. retention.py NEVER touches these ledgers (it refuses
-- audit_log* targets; it only anonymizes ingest_events), so the trigger does not
-- interfere with GDPR.

CREATE OR REPLACE FUNCTION dse_ledger_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF current_user = 'dse'
       OR current_setting('dse.ledger_maintenance', true) = 'on' THEN
        RETURN COALESCE(NEW, OLD);  -- break-glass: DR/controlled retention/test
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

-- REVOKE of TRUNCATE (the REVOKE of UPDATE/DELETE already exists in 0001 for
-- audit_log; this reinforces it for both ledgers). TRUNCATE does not fire row
-- triggers, so the REVOKE is the only defense against it — hence it is essential
-- here.
REVOKE TRUNCATE ON audit_log FROM dse_app;
REVOKE TRUNCATE ON audit_log FROM PUBLIC;
REVOKE UPDATE, DELETE, TRUNCATE ON model_call_ledger FROM dse_app;
REVOKE UPDATE, DELETE, TRUNCATE ON model_call_ledger FROM PUBLIC;
