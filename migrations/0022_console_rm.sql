-- Fintex DSE — Plano 06 F0 — read model do console (schema console_rm).
-- Projecao DERIVADA do system of record (nunca dual-write): o worker
-- services/console-projector tailea work_items/ingest_events/audit_log/
-- model_call_ledger por cursores e materializa aqui o SHAPE que o
-- dse_console_pane consome. Drop schema + replay reconstroi tudo (P8).
-- Migracao aditiva e idempotente.

CREATE SCHEMA IF NOT EXISTS console_rm;

-- Shape `WorkItem` do console (status ja no vocabulario do console — o mapa
-- 17->11 vive no projector, testado por contrato).
CREATE TABLE IF NOT EXISTS console_rm.work_items_view (
    work_item_id  TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    source        TEXT NOT NULL,             -- github|jira|slack|teams|admin
    source_id     TEXT,                      -- ex.: andre2654/fintex-wallet#8
    repo          TEXT,
    base_branch   TEXT,
    title         TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL DEFAULT '',
    requester     TEXT,
    priority      TEXT NOT NULL DEFAULT 'normal',
    data_class    TEXT,
    status        TEXT NOT NULL,             -- vocabulario do console
    current_phase TEXT,
    last_event    TEXT,
    pr_number     INTEGER,
    pr_url        TEXT,
    model         TEXT,
    assigned_runtime TEXT,
    budget_usd    NUMERIC(14, 4),
    risk_class    TEXT,
    sla_due_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_crm_wi_tenant   ON console_rm.work_items_view (tenant_id);
CREATE INDEX IF NOT EXISTS idx_crm_wi_status   ON console_rm.work_items_view (status);
CREATE INDEX IF NOT EXISTS idx_crm_wi_updated  ON console_rm.work_items_view (updated_at DESC);

-- `TimelineEvent` do console, derivado 1:1 de audit_log (acoes mapeadas para
-- EventType; nao mapeadas viram `note` — nunca silenciar).
CREATE TABLE IF NOT EXISTS console_rm.timeline_events (
    audit_id     BIGINT NOT NULL,
    work_item_id TEXT   NOT NULL,
    tenant_id    TEXT   NOT NULL,
    type         TEXT   NOT NULL,
    channel      TEXT,
    message      TEXT   NOT NULL,
    data         JSONB  NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (work_item_id, audit_id)
);
CREATE INDEX IF NOT EXISTS idx_crm_tl_wi ON console_rm.timeline_events (work_item_id, created_at);

-- `RunRow` do console (analytics de custo/tokens). DUAS fontes (Plano 06,
-- achado F0): model_call_ledger (chamadas via client Python) E audit_log
-- details->>'cost_usd' dos *_turn_completed — o substrato in-process fala com
-- o gateway direto e nao passa pelo client que grava o ledger. run_key
-- desambigua: 'ledger:<id>' | 'audit:<id>'. Fix definitivo (gateway grava
-- server-side) e item F1.
CREATE TABLE IF NOT EXISTS console_rm.runs_view (
    run_key      TEXT PRIMARY KEY,
    work_item_id TEXT NOT NULL,
    tenant_id    TEXT NOT NULL,
    engine       TEXT NOT NULL,              -- stage (planner|coder|tester|l2...)
    model        TEXT,
    status       TEXT NOT NULL DEFAULT 'completed',
    tokens_in    BIGINT NOT NULL DEFAULT 0,
    tokens_out   BIGINT NOT NULL DEFAULT 0,
    cost_usd     NUMERIC(14, 8) NOT NULL DEFAULT 0,
    started_at   TIMESTAMPTZ NOT NULL,
    ended_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_crm_runs_tenant_started ON console_rm.runs_view (tenant_id, started_at);
CREATE INDEX IF NOT EXISTS idx_crm_runs_wi ON console_rm.runs_view (work_item_id);

-- Baselines de ROI (G1 do plano 06): decisao de negocio por tenant; sem linha,
-- os cards de savings do console mostram "—" (comportamento honesto da UI).
CREATE TABLE IF NOT EXISTS console_rm.baselines (
    tenant_id          TEXT PRIMARY KEY,
    hourly_rate_usd    NUMERIC(10, 2),
    default_task_hours NUMERIC(8, 2),
    per_task_class     JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- High-water marks das fontes (exactly-once efetivo: saida + cursor na MESMA
-- transacao; replay de lote e idempotente por upsert).
CREATE TABLE IF NOT EXISTS console_rm.projection_cursor (
    source     TEXT PRIMARY KEY,             -- audit_log | model_call_ledger | work_items | ingest_events
    last_id    BIGINT NOT NULL DEFAULT 0,
    last_seen  TIMESTAMPTZ,                  -- keyset (updated_at, id) p/ fontes mutaveis
    last_key   TEXT,                         -- desempate do keyset (PK TEXT de work_items)
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

GRANT USAGE ON SCHEMA console_rm TO dse_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA console_rm TO dse_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA console_rm
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO dse_app;
