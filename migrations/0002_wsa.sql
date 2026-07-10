-- Fintex DSE — Fase 1 — WS-A (Ingestão e adaptadores)
-- Dono: WS-A. Arquivo reservado (ver CONVENTIONS.md) — não editar fora do WS-A.
--
-- Três tabelas próprias do WS-A:
--   1. channel_kill_switches — kill switch por (tenant, canal) consultado pelo
--      gateway ANTES de admit_work_item (WSA-E1-T3). Granularidade mais fina
--      que `tenant_config.kill_switch_enabled` (WS-F, todo o tenant) — as duas
--      são checadas: tenant-wide primeiro (se existir), depois canal.
--   2. tenant_steering_allowlist — fallback explícito de quem pode "steerar"
--      uma tarefa em andamento via comentário (WSA-E6-T2a). Nunca "qualquer
--      um pode steerar" — ausência de linha = não autorizado.
--   3. comment_state — persistência do comment_ref por (work_item_id, surface)
--      usada pelo `dse_contracts.mutable_comment.MutableCommentWriter` para
--      manter os adapters 100% stateless (crash-consistent).

CREATE TABLE IF NOT EXISTS channel_kill_switches (
    tenant_id   TEXT NOT NULL,
    channel     TEXT NOT NULL,   -- ex.: slack channel id, github "owner/repo"
    active      BOOLEAN NOT NULL DEFAULT false,  -- true = canal DESLIGADO (bloqueia admissão)
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
