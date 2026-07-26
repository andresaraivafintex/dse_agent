-- Fintex DSE — remediation sprint 1 — operational truth of the WorkItem.
-- Additive and idempotent migration. The fields let Postgres project the
-- durable state of the workflow without inferring plan/risk/SHAs from the
-- audit_log or from mutable branch names.

ALTER TABLE work_items ADD COLUMN IF NOT EXISTS base_sha TEXT;
ALTER TABLE work_items ADD COLUMN IF NOT EXISTS head_sha TEXT;
ALTER TABLE work_items ADD COLUMN IF NOT EXISTS plan_hash TEXT;
ALTER TABLE work_items ADD COLUMN IF NOT EXISTS expected_files JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE work_items ADD COLUMN IF NOT EXISTS pr_url TEXT;
ALTER TABLE work_items ADD COLUMN IF NOT EXISTS ci_status TEXT;
ALTER TABLE work_items ADD COLUMN IF NOT EXISTS state_version BIGINT NOT NULL DEFAULT 0;
ALTER TABLE work_items ADD COLUMN IF NOT EXISTS last_error TEXT;
ALTER TABLE work_items ADD COLUMN IF NOT EXISTS validation_attempts JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE work_items ADD COLUMN IF NOT EXISTS last_transition_at TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'work_items_ci_status_check'
    ) THEN
        ALTER TABLE work_items ADD CONSTRAINT work_items_ci_status_check
            CHECK (ci_status IS NULL OR ci_status IN ('pending', 'green', 'red'));
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_work_items_base_sha ON work_items (base_sha)
    WHERE base_sha IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_work_items_head_sha ON work_items (head_sha)
    WHERE head_sha IS NOT NULL;

GRANT SELECT, UPDATE ON work_items TO dse_app;

INSERT INTO schema_migrations (filename) VALUES ('0021_wsb_operational_truth.sql')
ON CONFLICT DO NOTHING;
