-- Fintex DSE — Fase 1 — WS-F (Segurança, compliance, plataforma e operações)
-- Dono: WS-F. Arquivo reservado (ver CONVENTIONS.md) — não editar fora do WS-F.
--
-- tenant_config: parâmetros de fairness/budget/kill-switch por tenant, lidos
-- pelo orchestrator (WS-B) e pelo model-gateway (WS-D) antes de admitir/rodar
-- trabalho para um tenant. Fase 1: schema mínimo suficiente para o
-- kill-switch manual e o budget cap mensal por tenant (NFR de fairness fica
-- para Fase 2 — campo `fairness` já reservado como JSONB para não precisar de
-- nova migração quando isso for implementado).

CREATE TABLE IF NOT EXISTS tenant_config (
    tenant_id            TEXT PRIMARY KEY,
    monthly_budget_usd   NUMERIC(12, 2) NOT NULL DEFAULT 100.00,
    max_concurrent_work_items INTEGER NOT NULL DEFAULT 5,
    kill_switch_enabled  BOOLEAN NOT NULL DEFAULT false,
    kill_switch_reason   TEXT,
    fairness             JSONB NOT NULL DEFAULT '{}'::jsonb, -- reservado p/ Fase 2 (per-tenant weighting)
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_tenant_config_updated_at ON tenant_config;
CREATE TRIGGER trg_tenant_config_updated_at
    BEFORE UPDATE ON tenant_config
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Auditoria de mudanças de kill-switch/budget é feita pelo caller via
-- dse_audit.emit(...) no mesmo request/transação — esta tabela guarda apenas
-- o estado atual, não o histórico (o histórico vive no audit_log).

GRANT SELECT, INSERT, UPDATE ON tenant_config TO dse_app;

INSERT INTO schema_migrations (filename) VALUES ('0007_wsf.sql')
ON CONFLICT DO NOTHING;
