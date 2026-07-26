-- WS-C — sandbox-runtime / egress-proxy (Phase 1, WSC-E1/E2).
-- Owner: WS-C. Do not edit 0001_foundation.sql — this file is additive.
--
-- `sandbox_leases`: durable bookkeeping of sandbox containers per task
-- (complements the Docker labels — useful for reconciliation/dashboards
-- outside Docker itself, and for WS-F to correlate infra cost per tenant).
-- It is NOT the source of truth for container state (Docker is); it is just
-- a lifecycle event log.
--
-- `egress_credential_leases`: evidence of the ephemeral credential revocation
-- SLO (WSC-E2-T2, "within 60s") — each row is a credential minted by the
-- egress-proxy, with issued_at/revoked_at, so that we can prove in an audit
-- (P8) that no credential stayed alive beyond the end of the task.

CREATE TABLE IF NOT EXISTS sandbox_leases (
    id               BIGSERIAL PRIMARY KEY,
    work_item_id     TEXT NOT NULL,
    tenant_id        TEXT NOT NULL,
    container_id     TEXT,
    branch           TEXT NOT NULL,
    resource_class   TEXT NOT NULL DEFAULT 'small',
    status           TEXT NOT NULL DEFAULT 'provisioned'
                       CHECK (status IN ('provisioned', 'checkpointed', 'rebuilt', 'torn_down')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sandbox_leases_work_item ON sandbox_leases (work_item_id);

DROP TRIGGER IF EXISTS trg_sandbox_leases_updated_at ON sandbox_leases;
CREATE TRIGGER trg_sandbox_leases_updated_at
    BEFORE UPDATE ON sandbox_leases
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS egress_credential_leases (
    credential_id    TEXT PRIMARY KEY,
    work_item_id     TEXT NOT NULL,
    tenant_id        TEXT NOT NULL,
    repo             TEXT NOT NULL,
    branch           TEXT NOT NULL,
    allowed_actions  JSONB NOT NULL DEFAULT '[]'::jsonb,
    fixture          BOOLEAN NOT NULL DEFAULT true,
    issued_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at       TIMESTAMPTZ,
    revoke_latency_s DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_egress_credential_leases_work_item ON egress_credential_leases (work_item_id);
CREATE INDEX IF NOT EXISTS idx_egress_credential_leases_unrevoked
    ON egress_credential_leases (work_item_id) WHERE revoked_at IS NULL;

GRANT SELECT, INSERT, UPDATE ON sandbox_leases TO dse_app;
GRANT SELECT, INSERT, UPDATE ON egress_credential_leases TO dse_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO dse_app;

INSERT INTO schema_migrations (filename) VALUES ('0004_wsc.sql')
ON CONFLICT DO NOTHING;
