-- Fintex DSE — Phase 2 — WS-E (L2 fresh-context loop + strict PR mode)
-- Owner: WS-E (services/validation). Migration 0012 reserved for WS-E in Phase 2.
-- Do not edit 0001-0007 (foundation). This file only touches WS-E tables.
--
-- Additions:
--   wse_l2_reviews   — WSE-E2-T4: 1 row per run of the L2 Reviewer session
--                       (fresh context, only plan+diff — P3). Evidence of the
--                       verdict, of the objections and of the COST per iteration
--                       of the fix-retry loop (WSE-E2-T5), beyond the generic
--                       audit_log.
--   wse_fix_loops    — WSE-E2-T5: durable state of the bounded L2->Coder loop per
--                       WorkItem: number of iterations consumed and budget spent.
--                       The WS-B workflow owns the state orchestration; this
--                       table is the evidence/counter read by the deterministic
--                       decision logic of this workstream (no LLM decides).
--
-- Additive changes to wse_pr_tracking (created in 0006_wse.sql) for STRICT MODE
-- (WSE-E3-T8, unblocked by PrRef.compare_url in the Phase 2 contract):
--   - pr_number now accepts NULL (branch pushed + compare link posted, PR NOT
--     opened yet — a human opens it with 1 click);
--   - compare_url: compare link posted when pr_number IS NULL.
-- Additive and idempotent change: every Phase 1 caller keeps writing pr_number
-- filled in; nothing existing breaks.

CREATE TABLE IF NOT EXISTS wse_l2_reviews (
    id            BIGSERIAL PRIMARY KEY,
    work_item_id  TEXT NOT NULL,
    tenant_id     TEXT NOT NULL,
    iteration     INTEGER NOT NULL DEFAULT 0,
    passed        BOOLEAN NOT NULL,
    objections    JSONB NOT NULL DEFAULT '[]'::jsonb,
    cost_usd      NUMERIC(12, 6) NOT NULL DEFAULT 0,
    run_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_wse_l2_reviews_work_item
    ON wse_l2_reviews (work_item_id, run_at DESC);

CREATE TABLE IF NOT EXISTS wse_fix_loops (
    work_item_id   TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    iterations     INTEGER NOT NULL DEFAULT 0,   -- returns to the Coder already consumed
    spent_usd      NUMERIC(12, 6) NOT NULL DEFAULT 0,
    exhausted      BOOLEAN NOT NULL DEFAULT FALSE, -- true when escalated to an operator
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Strict mode (WSE-E3-T8): optional pr_number + compare_url.
ALTER TABLE wse_pr_tracking ALTER COLUMN pr_number DROP NOT NULL;
ALTER TABLE wse_pr_tracking ADD COLUMN IF NOT EXISTS compare_url TEXT;

GRANT SELECT, INSERT ON wse_l2_reviews TO dse_app;
GRANT SELECT, INSERT, UPDATE ON wse_fix_loops TO dse_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO dse_app;

INSERT INTO schema_migrations (filename) VALUES ('0012_wse2.sql')
ON CONFLICT DO NOTHING;
