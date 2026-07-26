-- Fintex DSE — Phase 2 — WS-B (Temporal orchestration)
-- Owner: WS-B. Reserved file (see CONVENTIONS.md, Phase 2 migrations table) —
-- do not edit outside WS-B.
--
-- plan_approval_gate: DURABLE and queryable record of the plan approval gate by
-- risk class (WSB-E3-T2/T3). The audit_log (append-only) already keeps the full
-- event history, but it is expensive to query in order to answer "which
-- WorkItems are RIGHT NOW parked waiting for approval, and by whom?" — a
-- question the queue board (WS-F, Phase 2) and operators ask all the time. This
-- table materializes that current state (1 row per work_item, idempotent upsert
-- by the workflow) without replacing the audit ledger.
--
-- P8: the audit_log remains the immutable source of truth; this table is a
-- mutable convenience projection. Every transition here is mirrored by a
-- dse_audit.emit(...) in the same Activity.

CREATE TABLE IF NOT EXISTS plan_approval_gate (
    work_item_id      TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL,
    risk_class        TEXT NOT NULL,                         -- EFFECTIVE class (policy.classify_risk)
    -- 'pending'  : parked, waiting for SIGNAL_PLAN_APPROVAL
    -- 'approved' : released (by a named human OR by policy auto-approval)
    -- 'rejected' : refused; see rejection_route
    -- 'blocked'  : EMPTY approver cascade (never auto-approves due to absence)
    status            TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'blocked')),
    auto_approved     BOOLEAN NOT NULL DEFAULT false,        -- true = released by policy (low risk), no human
    resolved_approvers JSONB NOT NULL DEFAULT '[]'::jsonb,   -- cascade CODEOWNERS -> access bundle
    decided_by        TEXT,                                  -- principal of the human who decided (NULL if auto/pending/blocked)
    rejection_route   TEXT CHECK (rejection_route IN ('re_plan', 're_clarify', 'cancel')),
    justification     TEXT,                                  -- mandatory on rejection (WSB-E3-T3)
    plan_round        INTEGER NOT NULL DEFAULT 0,            -- which Planner iteration (re_plan increments it)
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_plan_approval_gate_status ON plan_approval_gate (status);
CREATE INDEX IF NOT EXISTS idx_plan_approval_gate_tenant ON plan_approval_gate (tenant_id);

DROP TRIGGER IF EXISTS trg_plan_approval_gate_updated_at ON plan_approval_gate;
CREATE TRIGGER trg_plan_approval_gate_updated_at
    BEFORE UPDATE ON plan_approval_gate
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

GRANT SELECT, INSERT, UPDATE ON plan_approval_gate TO dse_app;

INSERT INTO schema_migrations (filename) VALUES ('0009_wsb2.sql')
ON CONFLICT DO NOTHING;
