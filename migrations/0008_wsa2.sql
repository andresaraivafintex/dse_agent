-- Fintex DSE — Phase 2 — WS-A (Jira, tenant mapping, merge, status-based routing)
-- Owner: WS-A. Reserved file (see CONVENTIONS.md §Phase 2) — do not edit outside WS-A.
--
-- Four tables owned by WS-A in Phase 2:
--   1. tenant_platform_bindings — mapping platform+binding_key -> tenant_id
--      (WSA-E1-T5). Resolves the tenant from the Slack workspace (team_id), the
--      GitHub App installation (installation_id) or the Jira site (cloud_id/
--      base_url). A missing binding falls back to DSE_TENANT_ID (single-tenant)
--      with a warning audit row — it never guesses a tenant.
--   2. jira_transition_queue — queue of Jira status transitions SERIALIZED per
--      ticket (WSA-E5-T3). Jira Cloud rejects concurrent transitions on the same
--      issue; the outbound worker processes one transition per ticket at a time
--      (advisory lock per ticket_key), in enqueue order.
--   3. jira_poll_state — cursor of the fallback poller (WSA-E5-T2). Stores the
--      last polled instant per (tenant_id, project_key) to detect drift between
--      what the (best-effort) webhook delivered and the real Jira state,
--      re-ingesting through the SAME idempotent path (dedup by event_id).

CREATE TABLE IF NOT EXISTS tenant_platform_bindings (
    platform     TEXT NOT NULL CHECK (platform IN ('slack', 'github', 'jira')),
    binding_key  TEXT NOT NULL,   -- slack team_id | github installation_id | jira cloud_id/base_url
    tenant_id    TEXT NOT NULL,
    note         TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (platform, binding_key)
);

DROP TRIGGER IF EXISTS trg_tenant_platform_bindings_updated_at ON tenant_platform_bindings;
CREATE TRIGGER trg_tenant_platform_bindings_updated_at
    BEFORE UPDATE ON tenant_platform_bindings
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS jira_transition_queue (
    id             BIGSERIAL PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    ticket_key     TEXT NOT NULL,           -- e.g.: "DSE-123"
    target_status  TEXT NOT NULL,           -- name of the target status (e.g.: "In Review")
    work_item_id   TEXT,                    -- optional correlation (audit)
    dedup_key      TEXT NOT NULL UNIQUE,     -- enqueue idempotency
    processed      BOOLEAN NOT NULL DEFAULT false,
    attempts       INT NOT NULL DEFAULT 0,
    last_error     TEXT,
    enqueued_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_jira_transition_queue_unprocessed
    ON jira_transition_queue (ticket_key, id) WHERE NOT processed;

CREATE TABLE IF NOT EXISTS jira_poll_state (
    tenant_id      TEXT NOT NULL,
    project_key    TEXT NOT NULL,
    last_polled_at TIMESTAMPTZ,             -- upper bound of the last polled window
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_key)
);

DROP TRIGGER IF EXISTS trg_jira_poll_state_updated_at ON jira_poll_state;
CREATE TRIGGER trg_jira_poll_state_updated_at
    BEFORE UPDATE ON jira_poll_state
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

GRANT SELECT, INSERT, UPDATE ON tenant_platform_bindings TO dse_app;
GRANT SELECT, INSERT, UPDATE ON jira_transition_queue TO dse_app;
GRANT USAGE, SELECT ON SEQUENCE jira_transition_queue_id_seq TO dse_app;
GRANT SELECT, INSERT, UPDATE ON jira_poll_state TO dse_app;

INSERT INTO schema_migrations (filename) VALUES ('0008_wsa2.sql')
ON CONFLICT DO NOTHING;
