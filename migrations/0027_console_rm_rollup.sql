-- Fintex DSE — Plan 08 §E — cost/usage rollup for the console Analytics.
-- Aggregates model_call_ledger (the source of truth for cost, P8) by
-- day×repo×model×task_class. Recomputed by the console-projector from the
-- SoR — the reconciliation test (CI) guarantees rollup == ledger. Additive and
-- idempotent.

-- task_class in the read model → "by category" charts (§E) and detail view.
ALTER TABLE console_rm.work_items_view ADD COLUMN IF NOT EXISTS task_class TEXT;

CREATE TABLE IF NOT EXISTS console_rm.cost_rollup (
    tenant_id   TEXT NOT NULL,
    day         DATE NOT NULL,
    repo        TEXT NOT NULL,              -- '(unknown)' when the ledger does not resolve a repo
    model       TEXT NOT NULL,
    task_class  TEXT NOT NULL,
    run_count   INTEGER NOT NULL DEFAULT 0,
    cost_usd    NUMERIC(16, 6) NOT NULL DEFAULT 0,
    tokens_in   BIGINT NOT NULL DEFAULT 0,
    tokens_out  BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, day, repo, model, task_class)
);
CREATE INDEX IF NOT EXISTS idx_crm_rollup_tenant_day ON console_rm.cost_rollup (tenant_id, day);
CREATE INDEX IF NOT EXISTS idx_crm_rollup_taskclass  ON console_rm.cost_rollup (tenant_id, task_class);

GRANT SELECT, INSERT, UPDATE, DELETE ON console_rm.cost_rollup TO dse_app;
