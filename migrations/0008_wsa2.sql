-- Fintex DSE — Fase 2 — WS-A (Jira, tenant mapping, merge, roteamento por status)
-- Dono: WS-A. Arquivo reservado (ver CONVENTIONS.md §Fase 2) — não editar fora do WS-A.
--
-- Quatro tabelas próprias do WS-A na Fase 2:
--   1. tenant_platform_bindings — mapeamento plataforma+binding_key -> tenant_id
--      (WSA-E1-T5). Resolve o tenant a partir do workspace Slack (team_id), da
--      installation da GitHub App (installation_id) ou do site Jira (cloud_id/
--      base_url). Binding ausente cai para DSE_TENANT_ID (single-tenant) com
--      audit row de aviso — nunca adivinha um tenant.
--   2. jira_transition_queue — fila de transições de status do Jira SERIALIZADA
--      por ticket (WSA-E5-T3). Jira Cloud rejeita transições concorrentes no
--      mesmo issue; o worker de saída processa uma transição por ticket de cada
--      vez (advisory lock por ticket_key), em ordem de enfileiramento.
--   3. jira_poll_state — cursor do poller de fallback (WSA-E5-T2). Guarda o
--      último instante consultado por (tenant_id, project_key) para detectar
--      drift entre o que o webhook (best-effort) entregou e o estado real do
--      Jira, re-ingerindo pela MESMA via idempotente (dedup por event_id).

CREATE TABLE IF NOT EXISTS tenant_platform_bindings (
    platform     TEXT NOT NULL CHECK (platform IN ('slack', 'github', 'jira')),
    binding_key  TEXT NOT NULL,   -- slack team_id | github installation_id | jira cloud_id/base_url
    tenant_id    TEXT NOT NULL,
    note         TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (platform, binding_key)
);

DROP TRIGGER IF EXISTS trg_tenant_platform_bindings_updated_at ON tenant_platform_bindings;
CREATE TRIGGER trg_tenant_platform_bindings_updated_at
    BEFORE UPDATE ON tenant_platform_bindings
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS jira_transition_queue (
    id             BIGSERIAL PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    ticket_key     TEXT NOT NULL,           -- ex.: "DSE-123"
    target_status  TEXT NOT NULL,           -- nome do status alvo (ex.: "In Review")
    work_item_id   TEXT,                    -- correlação opcional (auditoria)
    dedup_key      TEXT NOT NULL UNIQUE,     -- idempotência de enfileiramento
    processed      BOOLEAN NOT NULL DEFAULT false,
    attempts       INT NOT NULL DEFAULT 0,
    last_error     TEXT,
    enqueued_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_jira_transition_queue_unprocessed
    ON jira_transition_queue (ticket_key, id) WHERE NOT processed;

CREATE TABLE IF NOT EXISTS jira_poll_state (
    tenant_id      TEXT NOT NULL,
    project_key    TEXT NOT NULL,
    last_polled_at TIMESTAMPTZ,             -- limite superior da última janela consultada
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_key)
);

DROP TRIGGER IF EXISTS trg_jira_poll_state_updated_at ON jira_poll_state;
CREATE TRIGGER trg_jira_poll_state_updated_at
    BEFORE UPDATE ON jira_poll_state
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

GRANT SELECT, INSERT, UPDATE ON tenant_platform_bindings TO dse_app;
GRANT SELECT, INSERT, UPDATE ON jira_transition_queue TO dse_app;
GRANT USAGE, SELECT ON SEQUENCE jira_transition_queue_id_seq TO dse_app;
GRANT SELECT, INSERT, UPDATE ON jira_poll_state TO dse_app;

INSERT INTO schema_migrations (filename) VALUES ('0008_wsa2.sql')
ON CONFLICT DO NOTHING;
