-- Fintex DSE — Phase 3 — WS-B (Temporal orchestration)
-- Owner: WS-B. Reserved file (see CONVENTIONS.md, Phase 3 migrations table) —
-- do not edit outside WS-B.
--
-- work_item_evidence: DURABLE and queryable projection of the state of the
-- evidence pipeline (Phase 3 — Argo CD preview + Playwright demo + visual diff,
-- ADR-26/ADR-27). The audit_log (append-only) keeps the full history of every
-- refresh; this table materializes the CURRENT state (1 row per work_item,
-- idempotent upsert by the workflow) so that the queue board (WS-F) and
-- operators can answer "what is the latest preview/evidence for this PR?"
-- without scanning the ledger.
--
-- P8: the audit_log remains the immutable source of truth; every transition here
-- is mirrored by a dse_audit.emit(...) in the workflow (actions preview_triggered,
-- demo_evidence_completed, visual_diff_completed, evidence_degraded,
-- evidence_skipped_backend_only, evidence_refresh_declined_cap).

CREATE TABLE IF NOT EXISTS work_item_evidence (
    work_item_id        TEXT PRIMARY KEY,
    tenant_id           TEXT NOT NULL,
    -- PreviewRef.status from the contract: created | skipped_backend_only | degraded
    -- (NULL = the pipeline never ran, or failed before trigger_preview returned)
    preview_status      TEXT,
    preview_url         TEXT,
    demo_passed         BOOLEAN,                -- DemoEvidenceResult.passed
    video_artifact_key  TEXT,                   -- key in the artifact store (Garage, WS-E)
    trace_artifact_key  TEXT,
    visual_baseline_key TEXT,                   -- visual diff baseline (the 1st run creates it)
    refresh_count       INTEGER NOT NULL DEFAULT 0,  -- refreshes BEYOND the initial one (ADR-26, capped)
    -- initial | fix_cycle | fix_cycle_ci_red | human_request (ADR-26 debounce:
    -- refresh ONLY on a commit that changes behavior or on an explicit human request)
    last_refresh_reason TEXT,
    detail              TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_work_item_evidence_tenant ON work_item_evidence (tenant_id);
CREATE INDEX IF NOT EXISTS idx_work_item_evidence_status ON work_item_evidence (preview_status);

DROP TRIGGER IF EXISTS trg_work_item_evidence_updated_at ON work_item_evidence;
CREATE TRIGGER trg_work_item_evidence_updated_at
    BEFORE UPDATE ON work_item_evidence
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

GRANT SELECT, INSERT, UPDATE ON work_item_evidence TO dse_app;

INSERT INTO schema_migrations (filename) VALUES ('0014_wsb3.sql')
ON CONFLICT DO NOTHING;
