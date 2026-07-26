-- Fintex DSE — Phase 4 ("Loop hardening & learning") — WS-E (services/validation).
-- Migration 0020 reserved for WS-E in Phase 4 (addendum 03 §4; WS-E statement).
-- Do not edit 0001-0019 (foundation/Phases 1-3, and 0019 belongs to WS-C). This
-- file only creates a WS-E table. Additive and idempotent.
--
-- Additions:
--   wse_base_updates — WSE-E6-T16 (merge-base, never rebase during review):
--                      1 EVIDENCE row (P8) per run of update_base_branch.
--                      Records the chosen strategy (merge_base / rebase_prefirst_review
--                      / noop_no_drift), whether there was an unresolvable conflict
--                      (escalated to a human — the agent NEVER force-resolves) and the
--                      Phase 4 exit assertion: `orphaned_threads` — MUST be 0 on the
--                      merge-base path (review threads anchored to commits are
--                      preserved; a rebase+force-push orphans them — failure mode 11).
--
-- REVIEW FEEDBACK skill-learning episodes (WSE-E6-T18) do NOT get a new table:
-- they use `skill_episode` (source='review_feedback') from migration 0019 (WS-C) —
-- the "episode only, no skill created/activated" boundary is the same as for the
-- CI-repair episodes (wse_ci_repair_episodes, Phase 3). WS-C consumes the episodes
-- in the promotion pipeline (WSC-E4-T2/T3).

CREATE TABLE IF NOT EXISTS wse_base_updates (
    id                       BIGSERIAL PRIMARY KEY,
    work_item_id             TEXT NOT NULL,
    tenant_id                TEXT NOT NULL,
    repo                     TEXT NOT NULL,
    branch                   TEXT NOT NULL,
    base_branch              TEXT NOT NULL,
    strategy                 TEXT NOT NULL CHECK (strategy IN
                                 ('merge_base', 'rebase_prefirst_review', 'noop_no_drift')),
    conflict                 BOOLEAN NOT NULL DEFAULT FALSE,
    orphaned_threads         INTEGER NOT NULL DEFAULT 0,
    anchored_threads         INTEGER NOT NULL DEFAULT 0,  -- number of anchored review threads observed
    first_human_review_done  BOOLEAN NOT NULL DEFAULT TRUE,
    old_tip_sha              TEXT NOT NULL DEFAULT '',
    new_tip_sha              TEXT NOT NULL DEFAULT '',
    detail                   TEXT NOT NULL DEFAULT '',
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_wse_base_updates_work_item
    ON wse_base_updates (work_item_id, created_at DESC);

GRANT SELECT, INSERT ON wse_base_updates TO dse_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO dse_app;

INSERT INTO schema_migrations (filename) VALUES ('0020_wse4.sql')
ON CONFLICT DO NOTHING;
