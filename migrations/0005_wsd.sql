-- Fintex DSE — Phase 1 — WS-D (model-gateway / LiteLLM)
-- Owner: WS-D. Tracks virtual keys issued by the LiteLLM proxy per
-- tenant/work_item/stage (WSD-E1-T3). Source of truth for "who holds a live
-- model credential right now" — LiteLLM stores the key itself (or in memory,
-- if running without its own `database_url` — see README); this table is the
-- DSE-side record for audit/reconciliation and to allow revoking per work_item
-- without storing the key in clear text.

CREATE TABLE IF NOT EXISTS virtual_keys (
    id                 BIGSERIAL PRIMARY KEY,
    tenant_id          TEXT NOT NULL,
    work_item_id       TEXT NOT NULL,
    stage              TEXT NOT NULL,          -- dse_contracts.gateway_contract.Stage
    key_alias          TEXT NOT NULL UNIQUE,   -- deterministic alias sent to LiteLLM (not the key itself)
    key_hash           TEXT NOT NULL UNIQUE,   -- sha256(virtual key) — allows lookup in revoke() without storing the key in clear text
    key_prefix         TEXT NOT NULL,          -- first characters of the virtual key (not the full secret, only for human debugging)
    status             TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
    issued_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_virtual_keys_tenant ON virtual_keys (tenant_id);
CREATE INDEX IF NOT EXISTS idx_virtual_keys_work_item ON virtual_keys (work_item_id);
CREATE INDEX IF NOT EXISTS idx_virtual_keys_status ON virtual_keys (status) WHERE status = 'active';

GRANT SELECT, INSERT, UPDATE ON virtual_keys TO dse_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO dse_app;

INSERT INTO schema_migrations (filename) VALUES ('0005_wsd.sql')
ON CONFLICT DO NOTHING;
