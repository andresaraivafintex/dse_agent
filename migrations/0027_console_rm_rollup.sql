-- Fintex DSE — Plano 08 §E — rollup de custo/uso para o Analytics do console.
-- Agrega model_call_ledger (a fonte de verdade de custo, P8) por
-- dia×repo×model×task_class. Recalculado pelo console-projector a partir do
-- SoR — o teste de reconciliação (CI) garante rollup == ledger. Aditiva e
-- idempotente.

-- task_class no read model → gráficos "por categoria" (§E) e detalhe.
ALTER TABLE console_rm.work_items_view ADD COLUMN IF NOT EXISTS task_class TEXT;

CREATE TABLE IF NOT EXISTS console_rm.cost_rollup (
    tenant_id   TEXT NOT NULL,
    day         DATE NOT NULL,
    repo        TEXT NOT NULL,              -- '(unknown)' quando o ledger não resolve repo
    model       TEXT NOT NULL,
    task_class  TEXT NOT NULL,
    run_count   INTEGER NOT NULL DEFAULT 0,
    cost_usd    NUMERIC(16, 6) NOT NULL DEFAULT 0,
    tokens_in   BIGINT NOT NULL DEFAULT 0,
    tokens_out  BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, day, repo, model, task_class)
);
CREATE INDEX IF NOT EXISTS idx_crm_rollup_tenant_day ON console_rm.cost_rollup (tenant_id, day);
CREATE INDEX IF NOT EXISTS idx_crm_rollup_taskclass  ON console_rm.cost_rollup (tenant_id, task_class);

GRANT SELECT, INSERT, UPDATE, DELETE ON console_rm.cost_rollup TO dse_app;
