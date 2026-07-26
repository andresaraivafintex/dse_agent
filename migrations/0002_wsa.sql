-- Fintex DSE — Phase 1 — WS-A (Ingestion and adapters)
-- Owner: WS-A. Reserved file (see CONVENTIONS.md) — do not edit outside WS-A.
--
-- Three tables owned by WS-A:
--   1. channel_kill_switches — kill switch per (tenant, channel) checked by the
--      gateway BEFORE admit_work_item (WSA-E1-T3). Finer grained than
--      `tenant_config.kill_switch_enabled` (WS-F, the whole tenant) — both are
--      checked: tenant-wide first (if present), then the channel.
--   2. tenant_steering_allowlist — explicit fallback for who may "steer" an
--      in-flight task via comment (WSA-E6-T2a). Never "anyone can steer" —
--      absence of a row = not authorized.
--   3. comment_state — persistence of the comment_ref per (work_item_id, surface)
--      used by `dse_contracts.mutable_comment.MutableCommentWriter` to keep
--      the adapters 100% stateless (crash-consistent).

CREATE TABLE IF NOT EXISTS channel_kill_switches (
    tenant_id   TEXT NOT NULL,
    channel     TEXT NOT NULL,   -- e.g.: slack channel id, github "owner/repo"
    active      BOOLEAN NOT NULL DEFAULT false,  -- true = channel TURNED OFF (blocks admission)
    reason      TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, channel)
);

DROP TRIGGER IF EXISTS trg_channel_kill_switches_updated_at ON channel_kill_switches;
CREATE TRIGGER trg_channel_kill_switches_updated_at
    BEFORE UPDATE ON channel_kill_switches
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS tenant_steering_allowlist (
    tenant_id     TEXT NOT NULL,
    principal_id  TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, principal_id)
);

CREATE TABLE IF NOT EXISTS comment_state (
    work_item_id  TEXT NOT NULL REFERENCES work_items (id),
    surface       TEXT NOT NULL,   -- 'slack' | 'github'
    comment_ref   TEXT NOT NULL,   -- JSON-encoded opaque ref (ts+channel | repo+issue+comment_id)
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (work_item_id, surface)
);

DROP TRIGGER IF EXISTS trg_comment_state_updated_at ON comment_state;
CREATE TRIGGER trg_comment_state_updated_at
    BEFORE UPDATE ON comment_state
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

GRANT SELECT, INSERT, UPDATE ON channel_kill_switches TO dse_app;
GRANT SELECT, INSERT, UPDATE ON tenant_steering_allowlist TO dse_app;
GRANT SELECT, INSERT, UPDATE ON comment_state TO dse_app;

INSERT INTO schema_migrations (filename) VALUES ('0002_wsa.sql')
ON CONFLICT DO NOTHING;
