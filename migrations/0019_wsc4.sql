-- Fintex DSE — Phase 4 — Skill promotion pipeline (WSC-E4-T3).
-- Owner: WS-C. Phase 4 entry gate (addendum 03 §4): migration 0010 created
-- skill_registry.status with CHECK (status IN ('approved','draft','retired')).
-- The candidate -> eval -> approved -> canary -> active (+ rolled_back) track
-- requires new states. Additive and idempotent migration.

-- 1) Widens the status CHECK. Postgres has no "ALTER CHECK"; drop and recreate
--    the constraint by name. The default name Postgres generated for the inline
--    CHECK in 0010 is `skill_registry_status_check`.
ALTER TABLE skill_registry DROP CONSTRAINT IF EXISTS skill_registry_status_check;
ALTER TABLE skill_registry ADD CONSTRAINT skill_registry_status_check
    CHECK (status IN ('draft', 'candidate', 'approved', 'canary', 'active', 'rolled_back', 'retired'));

-- 2) Versioning + provenance of the promotion. `version` allows a rollback by
--    pointer change (failure mode 13) without losing the previous version.
ALTER TABLE skill_registry ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;

-- 3) Skill-learning episodes (WSC-E4-T2) — the three "sources at launch"
--    (§10.17): recurring clarification (WS-B), CI-repair (WS-E), accepted review
--    feedback (WS-E). Full provenance, tenant-scoped. NO skill is created or
--    activated from here — it is only the governable input.
CREATE TABLE IF NOT EXISTS skill_episode (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    source        TEXT NOT NULL CHECK (source IN ('clarification', 'ci_repair', 'review_feedback')),
    work_item_id  TEXT,
    pattern_key   TEXT NOT NULL,          -- groups occurrences of the same pattern
    occurrence_n  INTEGER NOT NULL DEFAULT 1,
    provenance    JSONB NOT NULL DEFAULT '{}'::jsonb,  -- PR, reviewer, diff, etc.
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_skill_episode_tenant_pattern
    ON skill_episode (tenant_id, pattern_key);

-- 4) Eval trail of each candidate (WSC-E4-T3) — replay against the eval set.
CREATE TABLE IF NOT EXISTS skill_eval (
    id                 BIGSERIAL PRIMARY KEY,
    tenant_id          TEXT NOT NULL,
    skill_key          TEXT NOT NULL,
    candidate_version  INTEGER NOT NULL,
    passed             BOOLEAN NOT NULL,
    score              DOUBLE PRECISION NOT NULL DEFAULT 0,
    positive_hits      INTEGER NOT NULL DEFAULT 0,
    negative_regressions INTEGER NOT NULL DEFAULT 0,
    detail             TEXT NOT NULL DEFAULT '',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_skill_eval_key
    ON skill_eval (tenant_id, skill_key, candidate_version);

GRANT SELECT, INSERT, UPDATE ON skill_registry TO dse_app;
GRANT SELECT, INSERT ON skill_episode, skill_eval TO dse_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO dse_app;

INSERT INTO schema_migrations (filename) VALUES ('0019_wsc4.sql') ON CONFLICT DO NOTHING;
