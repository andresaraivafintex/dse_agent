-- Fintex DSE — Phase 2 — WS-D (model-gateway): call-time policy/budget,
-- gateway kill switch, durable cost ledger.
-- Owner: WS-D. Reserved file (see CONVENTIONS.md §"Phase 2 reserved
-- migrations") — do not edit outside WS-D.
--
-- Context: Phase 1 only issued virtual keys and aggregated cost in memory per
-- process. Phase 2 adds call-time enforcement (WSD-E2), a 4-scope kill switch
-- + model reassign (WSD-E4-T2) and a DURABLE cost source (WSD-E3-T4) that
-- survives a process restart — all of the tables below.

-- ---------------------------------------------------------------------------
-- WSD-E2-T1 — Per-stage/per-tenant policy engine (declarative config).
-- Maps (tenant, stage, data_class, risk_class) -> set of allowed models +
-- preferred model. `tenant_id`/`stage`/`data_class`/`risk_class` accept the
-- wildcard '*' (fallback). Resolution: the most specific row (fewest wildcards)
-- with the highest `priority` wins. Hot-reload: the engine reads this table at
-- call time (with a short TTL cache, default 5s — see policy.py), so an
-- operator doing UPDATE/INSERT here changes the policy without a redeploy and
-- the effect shows up in <TTL seconds.
CREATE TABLE IF NOT EXISTS model_policies (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       TEXT NOT NULL DEFAULT '*',   -- '*' = default for all tenants
    stage           TEXT NOT NULL DEFAULT '*',   -- dse_contracts.gateway_contract.Stage or '*'
    data_class      TEXT NOT NULL DEFAULT '*',   -- e.g.: 'internal' | 'restricted' | '*'
    risk_class      TEXT NOT NULL DEFAULT '*',   -- dse_contracts.plan_artifact.risk_class or '*'
    allowed_models  JSONB NOT NULL,              -- list of allowed model_name values (LiteLLM aliases)
    preferred_model TEXT,                        -- default/recommended model for this scope (stronger for coder, cheaper for reviewer)
    priority        INTEGER NOT NULL DEFAULT 0,  -- tie-breaker between rows of the same specificity
    is_active       BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, stage, data_class, risk_class)
);

DROP TRIGGER IF EXISTS trg_model_policies_updated_at ON model_policies;
CREATE TRIGGER trg_model_policies_updated_at
    BEFORE UPDATE ON model_policies
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX IF NOT EXISTS idx_model_policies_lookup
    ON model_policies (tenant_id, stage) WHERE is_active;

-- ---------------------------------------------------------------------------
-- WSD-E3-T4 — DURABLE cost ledger. Every successful model call writes a row
-- here (REAL cost/tokens returned by LiteLLM). It is the source of truth for
-- (a) cost aggregation that survives a restart (cost_export) and (b) call-time
-- budget accounting (spent-so-far sum). OTel spans keep being exported in
-- parallel to the WS-F collector (dashboards), but the local collector only
-- does `debug`/stdout (no queryable backend in this environment), so the
-- durable queryable source is this table.
CREATE TABLE IF NOT EXISTS model_call_ledger (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    work_item_id    TEXT NOT NULL,
    stage           TEXT NOT NULL,
    task_class      TEXT NOT NULL DEFAULT 'default',
    model           TEXT NOT NULL,
    cost_usd        NUMERIC(14, 8) NOT NULL DEFAULT 0,
    tokens_in       INTEGER NOT NULL DEFAULT 0,
    tokens_out      INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_model_call_ledger_tenant ON model_call_ledger (tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_model_call_ledger_work_item ON model_call_ledger (work_item_id);

-- ---------------------------------------------------------------------------
-- WSD-E2-T2 — Runtime budget per WorkItem. The AGGREGATE tenant budget lives in
-- `tenant_config.monthly_budget_usd` (WS-F, migrations/0007_wsf.sql) — we do not
-- duplicate it here. This table is only the per-WorkItem cap (spent-so-far comes
-- from the ledger above).
CREATE TABLE IF NOT EXISTS work_item_budgets (
    work_item_id    TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    max_budget_usd  NUMERIC(12, 4) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_work_item_budgets_updated_at ON work_item_budgets;
CREATE TRIGGER trg_work_item_budgets_updated_at
    BEFORE UPDATE ON work_item_budgets
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- WSD-E4-T2 — Gateway kill switch by scope. Matrix of 4 scopes
-- (global | tenant | work_item | channel). The gateway enforces at call time the
-- scopes visible in the call headers (global/tenant/work_item); rows with the
-- `channel` scope are honored at admission by WS-A/WS-B (the gateway does not
-- see the channel), documented in the README. Effect <60s: the engine reads with
-- a short TTL cache.
CREATE TABLE IF NOT EXISTS gateway_kill_switches (
    id              BIGSERIAL PRIMARY KEY,
    scope_type      TEXT NOT NULL CHECK (scope_type IN ('global', 'tenant', 'work_item', 'channel')),
    scope_id        TEXT NOT NULL DEFAULT '*',   -- '*' for global; tenant_id/work_item_id/channel_id otherwise
    enabled         BOOLEAN NOT NULL DEFAULT true,
    reason          TEXT,
    actor           TEXT,                        -- principal of the operator who triggered it (P8)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (scope_type, scope_id)
);

DROP TRIGGER IF EXISTS trg_gateway_kill_switches_updated_at ON gateway_kill_switches;
CREATE TRIGGER trg_gateway_kill_switches_updated_at
    BEFORE UPDATE ON gateway_kill_switches
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX IF NOT EXISTS idx_gateway_kill_switches_enabled
    ON gateway_kill_switches (scope_type, scope_id) WHERE enabled;

-- ---------------------------------------------------------------------------
-- WSD-E4-T2 — Model reassign for an in-flight task. An operator swaps the
-- effective model of a WorkItem; the next call for that work_item uses
-- `to_model` instead of the requested one (still subject to policy — reassign
-- does not bypass policy). At most one active reassignment per work_item.
CREATE TABLE IF NOT EXISTS model_reassignments (
    id              BIGSERIAL PRIMARY KEY,
    work_item_id    TEXT NOT NULL,
    to_model        TEXT NOT NULL,
    reason          TEXT,
    actor           TEXT,
    is_active       BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Only one active reassignment per work_item (unique partial index).
CREATE UNIQUE INDEX IF NOT EXISTS uq_model_reassignments_active
    ON model_reassignments (work_item_id) WHERE is_active;

-- ---------------------------------------------------------------------------
-- Grants (same dse_app role as the rest of the control-plane).
GRANT SELECT, INSERT, UPDATE ON model_policies, work_item_budgets,
    gateway_kill_switches, model_reassignments TO dse_app;
GRANT SELECT, INSERT ON model_call_ledger TO dse_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO dse_app;

INSERT INTO schema_migrations (filename) VALUES ('0011_wsd2.sql')
ON CONFLICT DO NOTHING;
