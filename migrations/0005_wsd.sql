-- Fintex DSE — Fase 1 — WS-D (model-gateway / LiteLLM)
-- Dono: WS-D. Rastreia virtual keys emitidas pelo LiteLLM proxy por
-- tenant/work_item/stage (WSD-E1-T3). Fonte de verdade para "quem tem uma
-- credencial de modelo viva agora" — o LiteLLM guarda a key em si (ou em
-- memória, se rodando sem `database_url` próprio — ver README), esta tabela
-- é o registro do lado do DSE para auditoria/reconciliação e para permitir
-- revogar por work_item mesmo sem guardar a key em claro.

CREATE TABLE IF NOT EXISTS virtual_keys (
    id                 BIGSERIAL PRIMARY KEY,
    tenant_id          TEXT NOT NULL,
    work_item_id       TEXT NOT NULL,
    stage              TEXT NOT NULL,          -- dse_contracts.gateway_contract.Stage
    key_alias          TEXT NOT NULL UNIQUE,   -- alias determinístico enviado ao LiteLLM (não é a key em si)
    key_hash           TEXT NOT NULL UNIQUE,   -- sha256(virtual key) — permite lookup em revoke() sem guardar a key em claro
    key_prefix         TEXT NOT NULL,          -- primeiros caracteres da virtual key (não segredo completo, só p/ debug humano)
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
