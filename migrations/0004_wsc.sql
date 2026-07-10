-- WS-C — sandbox-runtime / egress-proxy (Fase 1, WSC-E1/E2).
-- Dono: WS-C. Não editar 0001_foundation.sql — este arquivo é aditivo.
--
-- `sandbox_leases`: bookkeeping durável de containers de sandbox por tarefa
-- (complementa os labels do Docker — útil para reconciliação/dashboards
-- fora do próprio Docker, e para o WS-F correlacionar custo de infra por
-- tenant). Não é a fonte de verdade de estado do container (o Docker é);
-- é só um log de eventos de lifecycle.
--
-- `egress_credential_leases`: evidência do SLO de revogação de credencial
-- efêmera (WSC-E2-T2, "até 60s") — cada linha é uma credencial mintada pelo
-- egress-proxy, com issued_at/revoked_at, para poder provar em auditoria
-- (P8) que nenhuma credencial ficou viva além do fim da tarefa.

CREATE TABLE IF NOT EXISTS sandbox_leases (
    id               BIGSERIAL PRIMARY KEY,
    work_item_id     TEXT NOT NULL,
    tenant_id        TEXT NOT NULL,
    container_id     TEXT,
    branch           TEXT NOT NULL,
    resource_class   TEXT NOT NULL DEFAULT 'small',
    status           TEXT NOT NULL DEFAULT 'provisioned'
                       CHECK (status IN ('provisioned', 'checkpointed', 'rebuilt', 'torn_down')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sandbox_leases_work_item ON sandbox_leases (work_item_id);

DROP TRIGGER IF EXISTS trg_sandbox_leases_updated_at ON sandbox_leases;
CREATE TRIGGER trg_sandbox_leases_updated_at
    BEFORE UPDATE ON sandbox_leases
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS egress_credential_leases (
    credential_id    TEXT PRIMARY KEY,
    work_item_id     TEXT NOT NULL,
    tenant_id        TEXT NOT NULL,
    repo             TEXT NOT NULL,
    branch           TEXT NOT NULL,
    allowed_actions  JSONB NOT NULL DEFAULT '[]'::jsonb,
    fixture          BOOLEAN NOT NULL DEFAULT true,
    issued_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at       TIMESTAMPTZ,
    revoke_latency_s DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_egress_credential_leases_work_item ON egress_credential_leases (work_item_id);
CREATE INDEX IF NOT EXISTS idx_egress_credential_leases_unrevoked
    ON egress_credential_leases (work_item_id) WHERE revoked_at IS NULL;

GRANT SELECT, INSERT, UPDATE ON sandbox_leases TO dse_app;
GRANT SELECT, INSERT, UPDATE ON egress_credential_leases TO dse_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO dse_app;

INSERT INTO schema_migrations (filename) VALUES ('0004_wsc.sql')
ON CONFLICT DO NOTHING;
