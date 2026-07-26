-- Fintex DSE — Phase 3 — WS-F (reserved file, see CONVENTIONS.md §Phase 3)
-- WSF-E8-T2: retention by data classification (§12.2).
--
-- The per-tenant/per-class configurable policy lives in `tenant_config.retention`
-- (JSONB) — the table belongs to WS-F itself (0007_wsf.sql), so the ALTER below
-- does not touch another workstream's schema. Shape of the JSONB (validated in
-- dse_platform/retention.py, never "truly schema-less"):
--
--   {"<data_class>": {"days": <int > 0>}, ...}
--   e.g.: {"internal": {"days": 90}, "restricted": {"days": 30}}
--
-- A class WITHOUT an entry = NO purge (conservative by default: retention is an
-- explicit decision per tenant, never a silent default that deletes data).
--
-- audit_log is NOT covered by any policy here: it is append-only with its own
-- compliance-grade retention (0001_foundation.sql revokes UPDATE/DELETE even
-- from the app role — the guarantee is structural). dse_platform/retention.py
-- also refuses any 'audit_log%' target in code (defense in depth).

ALTER TABLE tenant_config
    ADD COLUMN IF NOT EXISTS retention JSONB NOT NULL DEFAULT '{}'::jsonb;

-- Supporting index for the purge job (it scans by age). A NEW object on top of
-- the foundation table (it does not edit 0001) — same rule as additive GRANTs.
CREATE INDEX IF NOT EXISTS idx_ingest_events_received_at
    ON ingest_events (received_at);

INSERT INTO schema_migrations (filename) VALUES ('0018_wsf3.sql')
ON CONFLICT DO NOTHING;
