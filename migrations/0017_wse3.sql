-- Fintex DSE — Phase 3 ("Evidence") — WS-E (services/validation).
-- Migration 0017 reserved for WS-E in Phase 3 (CONVENTIONS.md §Phase 3).
-- Do not edit 0001-0013 (foundation/Phases 1-2). This file only creates WS-E tables.
--
-- Additions:
--   wse_artifacts             — WSE-E5-T12: record of every artifact published to
--                               Garage (S3 key prefixed by WorkItem inside the
--                               per-tenant bucket, NFR-03). `quarantined_at` marks
--                               the access invalidation BEFORE the TTL when the
--                               work item goes into quarantine (stitches with WS-F,
--                               dse_work_item_quarantine).
--   wse_artifact_access_log   — WSE-E5-T12: 1 row per RESOLUTION of an evidence link
--                               (presign/open), associable to the PR — input to the
--                               "evidence consumption" metric.
--   wse_previews              — WSE-E4-T10: state of each preview environment per PR
--                               (ephemeral namespace in the k3d cluster, TTL, status).
--   wse_preview_caps          — ADR-26: cap of concurrent previews per tenant from
--                               day 1 (missing row => env default).
--   wse_ci_reruns             — WSE-E4-T9b: evidence of each targeted re-run requested
--                               on a fix commit (re-run only of the failed check-runs).
--   wse_ci_repair_episodes    — WSE-E4-T9b: skill-learning episodes of repeated
--                               CI-repair patterns (tenant-scoped, with provenance).
--                               NO skill is created/activated here — only the
--                               episode (skill promotion is Phase 4).
--   wse_evidence_publications — WSE-E5-T14/ADR-26: state of the evidence refresh
--                               debounce (last consolidated publication per WorkItem).

CREATE TABLE IF NOT EXISTS wse_artifacts (
    id              BIGSERIAL PRIMARY KEY,
    work_item_id    TEXT NOT NULL,
    tenant_id       TEXT NOT NULL,
    kind            TEXT NOT NULL,             -- demo_video | playwright_trace | visual_diff | visual_baseline | test_report
    bucket          TEXT NOT NULL,             -- per-tenant bucket (NFR-03)
    store_key       TEXT NOT NULL,             -- S3 key inside the bucket
    content_type    TEXT NOT NULL DEFAULT 'application/octet-stream',
    size_bytes      BIGINT NOT NULL DEFAULT 0,
    multipart       BOOLEAN NOT NULL DEFAULT FALSE,  -- multipart upload (revised ADR-18)
    ttl_seconds     INTEGER NOT NULL,
    expires_at      TIMESTAMPTZ NOT NULL,      -- POLICY expiry of the presigned link
    quarantined_at  TIMESTAMPTZ,               -- set when moved to the quarantine prefix
    quarantine_key  TEXT,                      -- new key under quarantine/ (access denied)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (bucket, store_key)
);

CREATE INDEX IF NOT EXISTS idx_wse_artifacts_work_item
    ON wse_artifacts (work_item_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_wse_artifacts_tenant
    ON wse_artifacts (tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS wse_artifact_access_log (
    id            BIGSERIAL PRIMARY KEY,
    artifact_id   BIGINT REFERENCES wse_artifacts (id),
    work_item_id  TEXT NOT NULL,
    tenant_id     TEXT NOT NULL,
    pr_number     INTEGER,                     -- associable to the PR (evidence consumption metric)
    accessor      TEXT NOT NULL,               -- resolved principal or system:<component>
    via           TEXT NOT NULL DEFAULT 'presign',  -- presign | tracking_comment | api
    accessed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_wse_artifact_access_work_item
    ON wse_artifact_access_log (work_item_id, accessed_at DESC);

CREATE TABLE IF NOT EXISTS wse_previews (
    work_item_id  TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    pr_number     INTEGER NOT NULL,
    repo          TEXT NOT NULL,
    status        TEXT NOT NULL,               -- created | skipped_backend_only | degraded | reaped
    namespace     TEXT,                        -- preview-<work_item_id> when created
    url           TEXT,
    detail        TEXT NOT NULL DEFAULT '',
    ttl_seconds   INTEGER NOT NULL DEFAULT 3600,
    expires_at    TIMESTAMPTZ,                 -- TTL of the ephemeral namespace
    reaped_at     TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_wse_previews_tenant_active
    ON wse_previews (tenant_id) WHERE status = 'created' AND reaped_at IS NULL;

CREATE TABLE IF NOT EXISTS wse_preview_caps (
    tenant_id       TEXT PRIMARY KEY,
    max_concurrent  INTEGER NOT NULL CHECK (max_concurrent >= 0)
);

CREATE TABLE IF NOT EXISTS wse_ci_reruns (
    id             BIGSERIAL PRIMARY KEY,
    work_item_id   TEXT NOT NULL,
    tenant_id      TEXT NOT NULL,
    pr_number      INTEGER NOT NULL,
    fix_commit_sha TEXT NOT NULL,
    check_run_ids  JSONB NOT NULL DEFAULT '[]'::jsonb,  -- re-run ids (only the failed ones)
    check_names    JSONB NOT NULL DEFAULT '[]'::jsonb,
    requested_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_wse_ci_reruns_work_item
    ON wse_ci_reruns (work_item_id, requested_at DESC);

CREATE TABLE IF NOT EXISTS wse_ci_repair_episodes (
    id                 BIGSERIAL PRIMARY KEY,
    tenant_id          TEXT NOT NULL,           -- tenant-scoped (never crosses tenants)
    work_item_id       TEXT NOT NULL,
    check_name         TEXT NOT NULL,
    failure_signature  TEXT NOT NULL,           -- deterministic signature of the failure pattern
    fix_commit_sha     TEXT NOT NULL,
    occurrence_n       INTEGER NOT NULL DEFAULT 1, -- number of times (tenant, signature) has already occurred
    provenance         JSONB NOT NULL DEFAULT '{}'::jsonb, -- pr, repo, shas, timestamps
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_wse_ci_repair_episodes_pattern
    ON wse_ci_repair_episodes (tenant_id, failure_signature, created_at DESC);

CREATE TABLE IF NOT EXISTS wse_evidence_publications (
    work_item_id     TEXT PRIMARY KEY,
    tenant_id        TEXT NOT NULL,
    last_commit_sha  TEXT NOT NULL DEFAULT '',
    fingerprint      TEXT NOT NULL DEFAULT '',  -- hash of the published links (avoids a no-op edit)
    published_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE ON wse_artifacts TO dse_app;
GRANT SELECT, INSERT ON wse_artifact_access_log TO dse_app;
GRANT SELECT, INSERT, UPDATE ON wse_previews TO dse_app;
GRANT SELECT, INSERT, UPDATE ON wse_preview_caps TO dse_app;
GRANT SELECT, INSERT ON wse_ci_reruns TO dse_app;
GRANT SELECT, INSERT ON wse_ci_repair_episodes TO dse_app;
GRANT SELECT, INSERT, UPDATE ON wse_evidence_publications TO dse_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO dse_app;

INSERT INTO schema_migrations (filename) VALUES ('0017_wse3.sql')
ON CONFLICT DO NOTHING;
