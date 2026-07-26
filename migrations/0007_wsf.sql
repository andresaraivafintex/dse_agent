-- Fintex DSE — Phase 1 — WS-F (Security, compliance, platform and operations)
-- Owner: WS-F. Reserved file (see CONVENTIONS.md) — do not edit outside WS-F.
--
-- tenant_config: fairness/budget/kill-switch parameters per tenant, read by the
-- orchestrator (WS-B) and by the model-gateway (WS-D) before admitting/running
-- work for a tenant. Phase 1: the minimum schema sufficient for the manual
-- kill-switch and the monthly budget cap per tenant (the fairness NFR is left
-- for Phase 2 — the `fairness` field is already reserved as JSONB so that no
-- new migration is needed when that is implemented).

CREATE TABLE IF NOT EXISTS tenant_config (
    tenant_id            TEXT PRIMARY KEY,
    monthly_budget_usd   NUMERIC(12, 2) NOT NULL DEFAULT 100.00,
    max_concurrent_work_items INTEGER NOT NULL DEFAULT 5,
    kill_switch_enabled  BOOLEAN NOT NULL DEFAULT false,
    kill_switch_reason   TEXT,
    fairness             JSONB NOT NULL DEFAULT '{}'::jsonb, -- reserved for Phase 2 (per-tenant weighting)
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_tenant_config_updated_at ON tenant_config;
CREATE TRIGGER trg_tenant_config_updated_at
    BEFORE UPDATE ON tenant_config
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Auditing of kill-switch/budget changes is done by the caller via
-- dse_audit.emit(...) in the same request/transaction — this table holds only
-- the current state, not the history (the history lives in audit_log).

GRANT SELECT, INSERT, UPDATE ON tenant_config TO dse_app;

INSERT INTO schema_migrations (filename) VALUES ('0007_wsf.sql')
ON CONFLICT DO NOTHING;
