-- Fintex DSE — Phase 2 ("Judgment & queue") — WS-F (security, compliance,
-- platform and operations).
-- Owner: WS-F. Reserved file (see CONVENTIONS.md, Phase 2 migrations table) —
-- do not edit outside WS-F. Additive on top of 0001–0007 (it does not change
-- any foundation table nor any other workstream's).
--
-- Tables:
--   1. dse_access_bundle       — WSF-E3-T2: access bundle per (tenant, channel)
--   2. dse_console_identity    — WSF-E3-T3: admin console users (SSO/ADR-22);
--                                offboarding removes them from approver/steering resolution
--   3. dse_kill_switch_global  — WSF-E6-T2: GLOBAL scope kill switch (the 4th scope;
--                                tenant=tenant_config, channel=channel_kill_switches[WS-A],
--                                task=Temporal signal)
--   4. dse_work_item_quarantine — WSF-E6-T2: durable quarantine of a work item
--                                (counterpart of the pause signal sent to the workflow)

-- ---------------------------------------------------------------------------
-- 1. dse_access_bundle (§10.18) — the manageable access "bundle" per
-- tenant/channel. A bundle with channel = NULL is the tenant default; a bundle
-- with channel filled in overrides the default for that specific channel
-- (resolution: channel-specific first, otherwise the tenant default).
--
-- Enforcement (dse_platform.access_bundles) is consulted at the decision points:
--   - allowed repos              -> intake/ingest-gateway (WS-A) and sandbox (WS-C)
--   - modes                      -> plan gate / flow selection (WS-B)
--   - blocked_actions            -> e.g.: direct_merge_to_protected_branch (WS-E/WS-B)
--   - designated_approvers       -> FALLBACK of the CODEOWNERS cascade of the
--                                   WS-B plan gate (WSB-E3-T2). An empty cascade
--                                   BLOCKS (it never auto-approves — P1/P3).
--   - learning_scope             -> skill promotion scope (WS-C, bootstrap)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dse_access_bundle (
    id                    BIGSERIAL PRIMARY KEY,
    tenant_id             TEXT NOT NULL,
    channel               TEXT,                  -- NULL = tenant default; otherwise a specific channel
    allowed_repos         JSONB NOT NULL DEFAULT '[]'::jsonb,   -- list of "owner/repo"; [] = none
    modes                 JSONB NOT NULL DEFAULT '[]'::jsonb,   -- subset of scope|ask|implement_low_risk|security_review
    budgets               JSONB NOT NULL DEFAULT '{}'::jsonb,   -- {"monthly_usd": .., "per_task_usd": ..}
    blocked_actions       JSONB NOT NULL DEFAULT '[]'::jsonb,   -- e.g.: ["direct_merge_to_protected_branch"]
    designated_approvers  JSONB NOT NULL DEFAULT '[]'::jsonb,   -- list of principal_id (cascade fallback)
    learning_scope        TEXT NOT NULL DEFAULT 'none'
                            CHECK (learning_scope IN ('none', 'tenant', 'global')),
    enabled               BOOLEAN NOT NULL DEFAULT true,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A single bundle per (tenant, channel). The tenant default (channel NULL)
-- needs its own unique partial index because NULL is not comparable in UNIQUE.
CREATE UNIQUE INDEX IF NOT EXISTS uq_access_bundle_tenant_channel
    ON dse_access_bundle (tenant_id, channel) WHERE channel IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_access_bundle_tenant_default
    ON dse_access_bundle (tenant_id) WHERE channel IS NULL;
CREATE INDEX IF NOT EXISTS idx_access_bundle_tenant ON dse_access_bundle (tenant_id);

DROP TRIGGER IF EXISTS trg_access_bundle_updated_at ON dse_access_bundle;
CREATE TRIGGER trg_access_bundle_updated_at
    BEFORE UPDATE ON dse_access_bundle
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- 2. dse_console_identity (WSF-E3-T3 / ADR-22) — admin console user
-- authenticated via SSO (OIDC/SAML). `sso_subject` is the IdP `sub` (account
-- matching by stable subject, not by email — see infra/ADR-22-identity.md).
-- `principal_id` links to the foundation identity map (dse_identity). `active`
-- false = offboarded: removed from approver/steering resolution AND from login.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dse_console_identity (
    principal_id     TEXT PRIMARY KEY REFERENCES principals (id),
    sso_subject      TEXT NOT NULL UNIQUE,       -- IdP `sub` claim (stable across logins)
    email            TEXT,
    display_name     TEXT,
    tenant_id        TEXT,                        -- "home" tenant (NULL = multi-tenant/platform operator)
    roles            JSONB NOT NULL DEFAULT '[]'::jsonb,   -- e.g.: ["operator","approver"]
    is_contractor    BOOLEAN NOT NULL DEFAULT false,       -- ADR-22: contractors expire
    active           BOOLEAN NOT NULL DEFAULT true,        -- false = offboarded
    expires_at       TIMESTAMPTZ,                 -- contractors: access expires (NULL = no expiry)
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    deactivated_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_console_identity_tenant ON dse_console_identity (tenant_id);
CREATE INDEX IF NOT EXISTS idx_console_identity_active ON dse_console_identity (active) WHERE active;

DROP TRIGGER IF EXISTS trg_console_identity_updated_at ON dse_console_identity;
CREATE TRIGGER trg_console_identity_updated_at
    BEFORE UPDATE ON dse_console_identity
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- 3. dse_kill_switch_global — the 4th kill switch scope (the broadest one).
-- Single row (id = 'global'). Enabled = ALL work admission, across every
-- tenant, is blocked. Consulted by the ingest-gateway (WS-A) and by the
-- model-gateway (WS-D) before the tenant/channel ones. Changes are always
-- audited (P8).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dse_kill_switch_global (
    id           TEXT PRIMARY KEY DEFAULT 'global' CHECK (id = 'global'),
    enabled      BOOLEAN NOT NULL DEFAULT false,
    reason       TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_kill_switch_global_updated_at ON dse_kill_switch_global;
CREATE TRIGGER trg_kill_switch_global_updated_at
    BEFORE UPDATE ON dse_kill_switch_global
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

INSERT INTO dse_kill_switch_global (id, enabled) VALUES ('global', false)
ON CONFLICT (id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 4. dse_work_item_quarantine — durable quarantine of a work item (counterpart
-- of the `pause`/`escalate` signal sent to the workflow by the operator). It
-- lives in the database so the decision survives a worker restart and so the
-- queue board projection shows "quarantined" even without querying the
-- workflow. released_at filled in = released.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dse_work_item_quarantine (
    work_item_id   TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    reason         TEXT NOT NULL,
    actor          TEXT NOT NULL,               -- principal of the operator (never the raw platform_user_id)
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    released_at    TIMESTAMPTZ,
    released_by    TEXT
);

CREATE INDEX IF NOT EXISTS idx_quarantine_tenant ON dse_work_item_quarantine (tenant_id);
CREATE INDEX IF NOT EXISTS idx_quarantine_active
    ON dse_work_item_quarantine (work_item_id) WHERE released_at IS NULL;

GRANT SELECT, INSERT, UPDATE ON dse_access_bundle TO dse_app;
GRANT SELECT, INSERT, UPDATE ON dse_console_identity TO dse_app;
GRANT SELECT, INSERT, UPDATE ON dse_kill_switch_global TO dse_app;
GRANT SELECT, INSERT, UPDATE ON dse_work_item_quarantine TO dse_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO dse_app;

INSERT INTO schema_migrations (filename) VALUES ('0013_wsf2.sql')
ON CONFLICT DO NOTHING;
