-- Fintex DSE — Phase 1 — WS-E (L1 validation + PR finalizer)
-- Owner: WS-E (services/validation). Do not edit 0001_foundation.sql; this file
-- is the only one WS-E touches inside the migrations/ directory.
--
-- Tables:
--   validation_runs   — 1 row per run of the L1 pipeline (WSE-E1), evidence of
--                        "passed"/findings beyond the generic audit_log (P8).
--   wse_pr_tracking   — 1 row per WorkItem with an open PR (WSE-E3-T6):
--                        guarantees EXACTLY 1 PR per work_item_id even if the
--                        finalizer process dies between creating the PR on the
--                        GitHub API and persisting the pr_number here (the
--                        finalizer always checks the GitHub API by head=branch
--                        before creating, in addition to this table — defense
--                        in depth).
--   wse_comment_refs  — CommentStateStore (dse_contracts.mutable_comment) for
--                        the GitHub backend of the PR finalizer (WSE-E3-T7).
--   wse_ci_status     — last known CI status per WorkItem/PR
--                        (WSE-E4-T9a), consumed by the WS-B workflow.

CREATE TABLE IF NOT EXISTS validation_runs (
    id            BIGSERIAL PRIMARY KEY,
    work_item_id  TEXT NOT NULL,
    tenant_id     TEXT NOT NULL,
    passed        BOOLEAN NOT NULL,
    findings      JSONB NOT NULL DEFAULT '[]'::jsonb,
    run_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_validation_runs_work_item
    ON validation_runs (work_item_id, run_at DESC);

CREATE TABLE IF NOT EXISTS wse_pr_tracking (
    work_item_id  TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    repo          TEXT NOT NULL,
    branch        TEXT NOT NULL,
    pr_number     INTEGER NOT NULL,
    pr_url        TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_wse_pr_tracking_repo_branch
    ON wse_pr_tracking (repo, branch);

CREATE TABLE IF NOT EXISTS wse_comment_refs (
    work_item_id  TEXT NOT NULL,
    surface       TEXT NOT NULL,
    comment_ref   TEXT NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (work_item_id, surface)
);

CREATE TABLE IF NOT EXISTS wse_ci_status (
    work_item_id  TEXT PRIMARY KEY,
    pr_number     INTEGER NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('pending', 'green', 'red')),
    detail        JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dse_app') THEN
        CREATE ROLE dse_app LOGIN PASSWORD 'dse_app_dev_only';
    END IF;
END
$$;

GRANT SELECT, INSERT ON validation_runs TO dse_app;
GRANT SELECT, INSERT, UPDATE ON wse_pr_tracking TO dse_app;
GRANT SELECT, INSERT, UPDATE ON wse_comment_refs TO dse_app;
GRANT SELECT, INSERT, UPDATE ON wse_ci_status TO dse_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO dse_app;

INSERT INTO schema_migrations (filename) VALUES ('0006_wse.sql')
ON CONFLICT DO NOTHING;
