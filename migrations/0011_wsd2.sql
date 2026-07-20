-- Fintex DSE — Fase 2 — WS-D (model-gateway): policy/budget no call time,
-- kill switch de gateway, ledger de custo durável.
-- Dono: WS-D. Arquivo reservado (ver CONVENTIONS.md §"Migrações reservadas da
-- Fase 2") — não editar fora do WS-D.
--
-- Contexto: a Fase 1 só emitia virtual keys e agregava custo em memória por
-- processo. A Fase 2 adiciona enforcement no call time (WSD-E2), kill switch de
-- 4 escopos + reassign de modelo (WSD-E4-T2) e uma fonte DURÁVEL de custo
-- (WSD-E3-T4) que sobrevive a restart do processo — todas as tabelas abaixo.

-- ---------------------------------------------------------------------------
-- WSD-E2-T1 — Motor de política per-stage/per-tenant (config declarativa).
-- Mapeia (tenant, stage, data_class, risk_class) -> conjunto de modelos
-- permitidos + modelo preferido. `tenant_id`/`stage`/`data_class`/`risk_class`
-- aceitam o coringa '*' (fallback). Resolução: a linha mais específica
-- (menor número de coringas) com maior `priority` vence. Hot-reload: o motor
-- lê esta tabela no call time (com um cache TTL curto, default 5s — ver
-- policy.py), então um operador que faz UPDATE/INSERT aqui muda a política
-- sem redeploy e o efeito aparece em <TTL segundos.
CREATE TABLE IF NOT EXISTS model_policies (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       TEXT NOT NULL DEFAULT '*',   -- '*' = default para todos os tenants
    stage           TEXT NOT NULL DEFAULT '*',   -- dse_contracts.gateway_contract.Stage ou '*'
    data_class      TEXT NOT NULL DEFAULT '*',   -- ex.: 'internal' | 'restricted' | '*'
    risk_class      TEXT NOT NULL DEFAULT '*',   -- dse_contracts.plan_artifact.risk_class ou '*'
    allowed_models  JSONB NOT NULL,              -- lista de model_name permitidos (LiteLLM aliases)
    preferred_model TEXT,                        -- modelo default/recomendado deste escopo (mais forte p/ coder, mais barato p/ reviewer)
    priority        INTEGER NOT NULL DEFAULT 0,  -- desempate entre linhas de mesma especificidade
    is_active       BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, stage, data_class, risk_class)
);

DROP TRIGGER IF EXISTS trg_model_policies_updated_at ON model_policies;
CREATE TRIGGER trg_model_policies_updated_at
    BEFORE UPDATE ON model_policies
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX IF NOT EXISTS idx_model_policies_lookup
    ON model_policies (tenant_id, stage) WHERE is_active;

-- ---------------------------------------------------------------------------
-- WSD-E3-T4 — Ledger de custo DURÁVEL. Cada chamada de modelo bem-sucedida
-- grava uma linha aqui (custo/tokens REAIS que o LiteLLM devolve). É a fonte
-- de verdade para (a) agregação de custo que sobrevive a restart (cost_export)
-- e (b) o accounting de budget no call time (soma spent-so-far). Os spans OTel
-- continuam sendo exportados em paralelo para o collector do WS-F (dashboards),
-- mas o collector local só faz `debug`/stdout (sem backend consultável neste
-- ambiente), então a fonte consultável durável é esta tabela.
CREATE TABLE IF NOT EXISTS model_call_ledger (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    work_item_id    TEXT NOT NULL,
    stage           TEXT NOT NULL,
    task_class      TEXT NOT NULL DEFAULT 'default',
    model           TEXT NOT NULL,
    cost_usd        NUMERIC(14, 8) NOT NULL DEFAULT 0,
    tokens_in       INTEGER NOT NULL DEFAULT 0,
    tokens_out      INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_model_call_ledger_tenant ON model_call_ledger (tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_model_call_ledger_work_item ON model_call_ledger (work_item_id);

-- ---------------------------------------------------------------------------
-- WSD-E2-T2 — Budget de runtime por WorkItem. O budget AGREGADO do tenant vive
-- em `tenant_config.monthly_budget_usd` (WS-F, migrations/0007_wsf.sql) — não
-- duplicamos aqui. Esta tabela é só o cap por WorkItem (spent-so-far vem do
-- ledger acima).
CREATE TABLE IF NOT EXISTS work_item_budgets (
    work_item_id    TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    max_budget_usd  NUMERIC(12, 4) NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_work_item_budgets_updated_at ON work_item_budgets;
CREATE TRIGGER trg_work_item_budgets_updated_at
    BEFORE UPDATE ON work_item_budgets
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- WSD-E4-T2 — Kill switch de gateway por escopo. Matriz de 4 escopos
-- (global | tenant | work_item | channel). O gateway enforça no call time os
-- escopos visíveis nos headers da chamada (global/tenant/work_item); linhas de
-- escopo `channel` são honradas na admissão pelo WS-A/WS-B (o gateway não vê o
-- canal), documentado no README. Efeito <60s: o motor lê com cache TTL curto.
CREATE TABLE IF NOT EXISTS gateway_kill_switches (
    id              BIGSERIAL PRIMARY KEY,
    scope_type      TEXT NOT NULL CHECK (scope_type IN ('global', 'tenant', 'work_item', 'channel')),
    scope_id        TEXT NOT NULL DEFAULT '*',   -- '*' para global; tenant_id/work_item_id/channel_id caso contrário
    enabled         BOOLEAN NOT NULL DEFAULT true,
    reason          TEXT,
    actor           TEXT,                        -- principal do operador que acionou (P8)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (scope_type, scope_id)
);

DROP TRIGGER IF EXISTS trg_gateway_kill_switches_updated_at ON gateway_kill_switches;
CREATE TRIGGER trg_gateway_kill_switches_updated_at
    BEFORE UPDATE ON gateway_kill_switches
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX IF NOT EXISTS idx_gateway_kill_switches_enabled
    ON gateway_kill_switches (scope_type, scope_id) WHERE enabled;

-- ---------------------------------------------------------------------------
-- WSD-E4-T2 — Reassign de modelo de uma tarefa em voo. Um operador troca o
-- modelo efetivo de um WorkItem; a próxima chamada daquele work_item usa
-- `to_model` no lugar do requisitado (ainda sujeito a policy — reassign não
-- burla a política). No máximo uma reassignment ativa por work_item.
CREATE TABLE IF NOT EXISTS model_reassignments (
    id              BIGSERIAL PRIMARY KEY,
    work_item_id    TEXT NOT NULL,
    to_model        TEXT NOT NULL,
    reason          TEXT,
    actor           TEXT,
    is_active       BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Só uma reassignment ativa por work_item (índice parcial único).
CREATE UNIQUE INDEX IF NOT EXISTS uq_model_reassignments_active
    ON model_reassignments (work_item_id) WHERE is_active;

-- ---------------------------------------------------------------------------
-- Grants (mesma role dse_app do resto do control-plane).
GRANT SELECT, INSERT, UPDATE ON model_policies, work_item_budgets,
    gateway_kill_switches, model_reassignments TO dse_app;
GRANT SELECT, INSERT ON model_call_ledger TO dse_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO dse_app;

INSERT INTO schema_migrations (filename) VALUES ('0011_wsd2.sql')
ON CONFLICT DO NOTHING;
