-- Fintex DSE — Phase 1 — Schema foundation (control plane + audit ledger + identity map)
-- Owner: foundation (contracts sprint). Do not edit — workstreams use 0002_*.sql onwards.
-- References: technical proposal §10.3 (work_items/ingest_events), §10.16 (audit_log),
-- §10.18/ADR-22 (identity map foundations, WSF-E3-T1).

CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- work_items — system of record for the lifecycle (WSA-E1-T1). id doubles as
-- the Temporal workflow_id (StartWorkflow(workflow_id = work_item_id)).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS work_items (
    id               TEXT PRIMARY KEY,
    tenant_id        TEXT NOT NULL,
    source           TEXT NOT NULL CHECK (source IN ('slack', 'github', 'jira')),
    source_ref       JSONB NOT NULL,              -- {thread_ts,channel} | {repo,issue_number} | {ticket_key}
    repo             TEXT,
    base_branch      TEXT,
    requester        TEXT NOT NULL,               -- resolved principal (dse_identity)
    data_class       TEXT NOT NULL DEFAULT 'internal',
    status           TEXT NOT NULL DEFAULT 'new', -- state machine §9.3
    risk_class       TEXT,                        -- 'low' | 'high' (approval gate is Phase 2)
    plan             JSONB,                        -- serialized PlanArtifact
    pr_number        INTEGER,
    budget           JSONB NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key  TEXT NOT NULL UNIQUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_work_items_tenant ON work_items (tenant_id);
CREATE INDEX IF NOT EXISTS idx_work_items_source_ref ON work_items USING GIN (source_ref);
CREATE INDEX IF NOT EXISTS idx_work_items_status ON work_items (status);

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_work_items_updated_at ON work_items;
CREATE TRIGGER trg_work_items_updated_at
    BEFORE UPDATE ON work_items
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- ingest_events — transactional outbox (WSA-E1-T1/T3). Written in the SAME
-- transaction as the corresponding work_items row (FR-06).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingest_events (
    id            BIGSERIAL PRIMARY KEY,
    work_item_id  TEXT NOT NULL REFERENCES work_items (id),
    event_id      TEXT NOT NULL UNIQUE,   -- sha256(platform+thread+message)
    kind          TEXT NOT NULL CHECK (kind IN
                     ('task_request','clarification_answer','approval','review_comment','steering')),
    payload       JSONB NOT NULL,
    processed     BOOLEAN NOT NULL DEFAULT false,
    received_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_ingest_events_unprocessed
    ON ingest_events (id) WHERE NOT processed;

-- ---------------------------------------------------------------------------
-- audit_log — append-only write sink (WSF-E1-T1). Partitioned by tenant
-- (NFR-03). REVOKE UPDATE/DELETE even for the application role.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id            BIGSERIAL,
    ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
    work_item_id  TEXT,
    tenant_id     TEXT NOT NULL,
    actor         TEXT NOT NULL,     -- resolved principal OR 'system:<component>'
    action        TEXT NOT NULL,
    details       JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (tenant_id, id)
) PARTITION BY LIST (tenant_id);

-- Default partition for tenants that do not have a dedicated one yet (onboarding creates theirs).
CREATE TABLE IF NOT EXISTS audit_log_default PARTITION OF audit_log DEFAULT;

CREATE OR REPLACE FUNCTION create_tenant_audit_partition(p_tenant_id TEXT) RETURNS VOID AS $$
DECLARE
    partition_name TEXT := 'audit_log_' || regexp_replace(p_tenant_id, '[^a-zA-Z0-9_]', '_', 'g');
BEGIN
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS %I PARTITION OF audit_log FOR VALUES IN (%L)',
        partition_name, p_tenant_id
    );
END;
$$ LANGUAGE plpgsql;

CREATE INDEX IF NOT EXISTS idx_audit_log_work_item ON audit_log (work_item_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log (ts);

-- Application role: INSERT/SELECT only. Never UPDATE/DELETE (not even the app
-- superuser should have the grant — the guarantee is structural, not a convention).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dse_app') THEN
        CREATE ROLE dse_app LOGIN PASSWORD 'dse_app_dev_only';
    END IF;
END
$$;

GRANT SELECT, INSERT ON work_items, ingest_events TO dse_app;
GRANT UPDATE ON work_items TO dse_app;              -- work_items has a legitimate update (status)
GRANT SELECT, UPDATE ON ingest_events TO dse_app;     -- mark processed=true

GRANT SELECT, INSERT ON audit_log TO dse_app;
REVOKE UPDATE, DELETE ON audit_log FROM dse_app;
REVOKE UPDATE, DELETE ON audit_log FROM PUBLIC;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO dse_app;

-- ---------------------------------------------------------------------------
-- Identity map foundations (WSF-E3-T1). Phase 1: resolution via self-registration
-- (no SSO/SCIM — that is ADR-22 / WSF-E3-T3, Phase 2).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS principals (
    id            TEXT PRIMARY KEY,      -- e.g.: 'usr_<uuid>'
    display_name  TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS identity_links (
    principal_id      TEXT NOT NULL REFERENCES principals (id),
    platform          TEXT NOT NULL CHECK (platform IN ('slack', 'github', 'jira')),
    platform_user_id  TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (platform, platform_user_id)
);

GRANT SELECT, INSERT ON principals, identity_links TO dse_app;

INSERT INTO schema_migrations (filename) VALUES ('0001_foundation.sql')
ON CONFLICT DO NOTHING;
