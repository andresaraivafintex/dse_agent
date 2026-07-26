-- Fintex DSE — Plan 06 F0 — console read model (console_rm schema).
-- DERIVED projection of the system of record (never dual-write): the worker
-- services/console-projector tails work_items/ingest_events/audit_log/
-- model_call_ledger by cursors and materializes here the SHAPE that
-- dse_console_pane consumes. Drop schema + replay rebuilds everything (P8).
-- Additive and idempotent migration.

CREATE SCHEMA IF NOT EXISTS console_rm;

-- The console's `WorkItem` shape (status already in the console vocabulary — the
-- 17->11 map lives in the projector, covered by a contract test).
CREATE TABLE IF NOT EXISTS console_rm.work_items_view (
    work_item_id  TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    source        TEXT NOT NULL,             -- github|jira|slack|teams|admin
    source_id     TEXT,                      -- e.g.: andre2654/fintex-wallet#8
    repo          TEXT,
    base_branch   TEXT,
    title         TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL DEFAULT '',
    requester     TEXT,
    priority      TEXT NOT NULL DEFAULT 'normal',
    data_class    TEXT,
    status        TEXT NOT NULL,             -- console vocabulary
    current_phase TEXT,
    last_event    TEXT,
    pr_number     INTEGER,
    pr_url        TEXT,
    model         TEXT,
    assigned_runtime TEXT,
    budget_usd    NUMERIC(14, 4),
    risk_class    TEXT,
    sla_due_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_crm_wi_tenant   ON console_rm.work_items_view (tenant_id);
CREATE INDEX IF NOT EXISTS idx_crm_wi_status   ON console_rm.work_items_view (status);
CREATE INDEX IF NOT EXISTS idx_crm_wi_updated  ON console_rm.work_items_view (updated_at DESC);

-- The console's `TimelineEvent`, derived 1:1 from audit_log (actions mapped to
-- EventType; unmapped ones become `note` — never silence anything).
CREATE TABLE IF NOT EXISTS console_rm.timeline_events (
    audit_id     BIGINT NOT NULL,
    work_item_id TEXT   NOT NULL,
    tenant_id    TEXT   NOT NULL,
    type         TEXT   NOT NULL,
    channel      TEXT,
    message      TEXT   NOT NULL,
    data         JSONB  NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (work_item_id, audit_id)
);
CREATE INDEX IF NOT EXISTS idx_crm_tl_wi ON console_rm.timeline_events (work_item_id, created_at);

-- The console's `RunRow` (cost/token analytics). TWO sources (Plan 06,
-- finding F0): model_call_ledger (calls through the Python client) AND audit_log
-- details->>'cost_usd' of the *_turn_completed events — the in-process substrate
-- talks to the gateway directly and does not go through the client that writes
-- the ledger. run_key disambiguates: 'ledger:<id>' | 'audit:<id>'. The definitive
-- fix (gateway writes server-side) is item F1.
CREATE TABLE IF NOT EXISTS console_rm.runs_view (
    run_key      TEXT PRIMARY KEY,
    work_item_id TEXT NOT NULL,
    tenant_id    TEXT NOT NULL,
    engine       TEXT NOT NULL,              -- stage (planner|coder|tester|l2...)
    model        TEXT,
    status       TEXT NOT NULL DEFAULT 'completed',
    tokens_in    BIGINT NOT NULL DEFAULT 0,
    tokens_out   BIGINT NOT NULL DEFAULT 0,
    cost_usd     NUMERIC(14, 8) NOT NULL DEFAULT 0,
    started_at   TIMESTAMPTZ NOT NULL,
    ended_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_crm_runs_tenant_started ON console_rm.runs_view (tenant_id, started_at);
CREATE INDEX IF NOT EXISTS idx_crm_runs_wi ON console_rm.runs_view (work_item_id);

-- ROI baselines (G1 of plan 06): a business decision per tenant; with no row,
-- the console savings cards show "—" (honest UI behavior).
CREATE TABLE IF NOT EXISTS console_rm.baselines (
    tenant_id          TEXT PRIMARY KEY,
    hourly_rate_usd    NUMERIC(10, 2),
    default_task_hours NUMERIC(8, 2),
    per_task_class     JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- High-water marks of the sources (effective exactly-once: output + cursor in the
-- SAME transaction; replaying a batch is idempotent via upsert).
CREATE TABLE IF NOT EXISTS console_rm.projection_cursor (
    source     TEXT PRIMARY KEY,             -- audit_log | model_call_ledger | work_items | ingest_events
    last_id    BIGINT NOT NULL DEFAULT 0,
    last_seen  TIMESTAMPTZ,                  -- keyset (updated_at, id) for mutable sources
    last_key   TEXT,                         -- keyset tie-breaker (TEXT PK of work_items)
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

GRANT USAGE ON SCHEMA console_rm TO dse_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA console_rm TO dse_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA console_rm
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO dse_app;
