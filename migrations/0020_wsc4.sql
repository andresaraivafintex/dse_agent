-- Fintex DSE — Phase 4 — WSC-E4-T2/T3: multi-version + provenance for the
-- skill promotion track. Owner: WS-C. ADDITIVE and idempotent migration.
--
-- Why it exists (the "something missing" of the Phase 4 gate): 0010 created
-- skill_registry with UNIQUE (tenant_id, skill_key) — ONE row per skill. 0019
-- added `version` and widened the status CHECK, but its comment promises
-- "rollback by pointer change WITHOUT LOSING THE PREVIOUS VERSION"
-- (failure mode 13) — that requires SEVERAL versions of the same skill to
-- coexist as distinct rows. Therefore uniqueness must move from (tenant, key)
-- to (tenant, key, version). On top of that, the materialized candidate (T2)
-- needs provenance (which pattern/episodes it came from) — new columns.

-- 1) Multi-version: swaps uniqueness from (tenant,key) to (tenant,key,version).
--    The default name from 0010 is skill_registry_tenant_id_skill_key_key.
ALTER TABLE skill_registry DROP CONSTRAINT IF EXISTS skill_registry_tenant_id_skill_key_key;
ALTER TABLE skill_registry DROP CONSTRAINT IF EXISTS skill_registry_tenant_key_version_key;
ALTER TABLE skill_registry ADD CONSTRAINT skill_registry_tenant_key_version_key
    UNIQUE (tenant_id, skill_key, version);

-- 2) STRUCTURAL invariant of what the Planner sees (P8 evidence-by-construction):
--    AT MOST one SERVED version per (tenant, skill_key). The Planner reads
--    status IN ('approved','active'); this unique partial index makes it
--    impossible for two served versions of the same skill to exist at the same
--    time — the transition to 'active' MUST demote the previously served version
--    in the same transaction, or the INSERT/UPDATE fails. Same for rollback.
DROP INDEX IF EXISTS uq_skill_registry_one_served;
CREATE UNIQUE INDEX uq_skill_registry_one_served
    ON skill_registry (tenant_id, skill_key)
    WHERE status IN ('approved', 'active');

-- 3) Provenance of the materialized candidate (WSC-E4-T2). Which pattern_key
--    (and which episodes/occurrences) the skill was born from — deterministic,
--    auditable.
ALTER TABLE skill_registry ADD COLUMN IF NOT EXISTS pattern_key TEXT;
ALTER TABLE skill_registry ADD COLUMN IF NOT EXISTS provenance JSONB NOT NULL DEFAULT '{}'::jsonb;

-- Index to quickly find the next version / the served version of a skill.
CREATE INDEX IF NOT EXISTS idx_skill_registry_tenant_key_version
    ON skill_registry (tenant_id, skill_key, version);

GRANT SELECT, INSERT, UPDATE ON skill_registry TO dse_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO dse_app;

INSERT INTO schema_migrations (filename) VALUES ('0020_wsc4.sql') ON CONFLICT DO NOTHING;
