-- Fintex DSE — Report 07 C2 (Phase B) — repository resolution for tasks that
-- do NOT originate inside a repo (Slack/Jira). Deterministic cascade (P1):
--   1. explicit override (repo= in the text / Jira field)  -> in the adapter
--   2. binding of the "natural unit of work" -> repo       -> THIS table
--   3. tenant single-repo default                          -> THIS table
--   4. ask (clarification)                                 -> workflow
-- The wrong repo never happens silently: with no resolution, the DSE asks.
--
-- binding_type orders precedence (most specific -> least):
--   channel   (Slack #channel)      \
--   component (Jira Component)        > specific
--   project   (Jira Project)        /
--   workspace (Slack team / Jira site) = broad fallback
-- Additive and idempotent migration.

CREATE TABLE IF NOT EXISTS repo_bindings (
    tenant_id     TEXT NOT NULL,
    platform      TEXT NOT NULL CHECK (platform IN ('slack', 'jira', 'github')),
    binding_type  TEXT NOT NULL CHECK (binding_type IN ('channel', 'component', 'project', 'workspace')),
    binding_value TEXT NOT NULL,             -- e.g.: 'C123' (channel), 'Payments' (component), 'FINX' (project)
    repo          TEXT NOT NULL,             -- e.g.: 'andre2654/fintex-wallet'
    base_branch   TEXT NOT NULL DEFAULT 'main',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, platform, binding_type, binding_value)
);
CREATE INDEX IF NOT EXISTS idx_repo_bindings_tenant ON repo_bindings (tenant_id);

-- single-repo default per tenant: when the tenant has exactly one repo, every
-- task with no other signal lands on it (removes friction for a single-repo
-- customer). The fixed binding_value '*' represents the default.
-- (no row is inserted here — populated during onboarding / by the console.)

CREATE TRIGGER set_updated_at_repo_bindings
    BEFORE UPDATE ON repo_bindings
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

GRANT SELECT, INSERT, UPDATE, DELETE ON repo_bindings TO dse_app;
